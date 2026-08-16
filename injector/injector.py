# -*- coding: utf-8 -*-
"""
injector.py — 纯 Python (ctypes) 注入器，把 Hook 脚本注入 Ren'Py 游戏进程

原理（正式实时注入路径，不改游戏任何文件）：
  1. 按 exe 名或 PID 找到游戏进程；
  2. 枚举目标进程模块，找到内嵌 Python DLL（python27.dll / python32.dll / python3xx.dll）；
  3. 从目标进程内存解析该 DLL 的 PE 导出表，定位 PyRun_SimpleString 的进程内地址；
  4. 在目标进程分配内存：脚本缓冲区 + 一小段机器码 stub（远程线程入口）；
  5. CreateRemoteThread 执行 stub -> stub 调用 PyRun_SimpleString(hook脚本) ->
     gamehook/hook.py 在游戏内运行，随后通过 TCP 与工具通信。

依赖：仅标准库 ctypes/struct/re，零编译。
注意：本注入器属正式路径，M0 阶段建议先用 tools/make_rpy_patch.py 的
      临时 .rpy 补丁验证 hook 逻辑，再真机联调本注入器。
"""

import ctypes
import os
import re
import struct
import sys
from ctypes import wintypes

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_ALL_ACCESS = 0x001F0FFF
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
INFINITE = 0xFFFFFFFF


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('th32ModuleID', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('GlblcntUsage', wintypes.DWORD),
        ('ProccntUsage', wintypes.DWORD),
        ('modBaseAddr', ctypes.c_void_p),
        ('modBaseSize', wintypes.DWORD),
        ('hModule', ctypes.c_void_p),
        ('szModule', ctypes.c_wchar * 256),
        ('szExePath', ctypes.c_wchar * 260),
    ]


# ---------------------------------------------------------------- 进程/模块查找

def find_process(target):
    """按 exe 文件名或 PID 找进程，返回 pid 或 None。"""
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        return int(target)
    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h == INVALID_HANDLE_VALUE:
        return None
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(h, ctypes.byref(e)):
            return None
        while True:
            if e.szExeFile.lower() == target.lower():
                return e.th32ProcessID
            if not kernel32.Process32NextW(h, ctypes.byref(e)):
                break
    finally:
        kernel32.CloseHandle(h)
    return None


def find_python_dll(pid):
    """在目标进程模块列表里找 Python DLL，返回 (name, base) 或 None。"""
    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if h == INVALID_HANDLE_VALUE:
        return None
    try:
        m = MODULEENTRY32W()
        m.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(h, ctypes.byref(m)):
            return None
        while True:
            name = m.szModule
            # Ren'Py 7: python27.dll / python32.dll；Ren'Py 8.1+：libpython3.12.dll 等
            if re.match(r'^(lib)?python\d+(\.\d+)?\.dll$', name, re.I):
                return (name, m.modBaseAddr)
            if not kernel32.Module32NextW(h, ctypes.byref(m)):
                break
    finally:
        kernel32.CloseHandle(h)
    return None


# ---------------------------------------------------------------- 目标进程内存读取

def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return None
    return buf.raw[:n.value]


def read_u16(h, addr):
    b = read_mem(h, addr, 2)
    return struct.unpack('<H', b)[0] if b else None


def read_u32(h, addr):
    b = read_mem(h, addr, 4)
    return struct.unpack('<I', b)[0] if b else None


def read_cstr(h, addr, max_len=256):
    b = read_mem(h, addr, max_len)
    if b is None:
        return None
    i = b.find(b'\x00')
    return b[:i].decode('ascii', 'ignore') if i >= 0 else b.decode('ascii', 'ignore')


def resolve_exports(h, dll_base, wanted):
    """解析目标进程内 dll_base 的 PE 导出表，返回 {名字: 进程内绝对地址}（只含 wanted 命中的项）。"""
    dos = read_mem(h, dll_base, 0x40)
    if not dos or dos[:2] != b'MZ':
        return {}
    pe_off = struct.unpack('<I', dos[0x3C:0x40])[0]
    pe = read_mem(h, dll_base + pe_off, 24)
    if not pe or pe[:4] != b'PE\x00\x00':
        return {}
    magic = read_u16(h, dll_base + pe_off + 24)
    if magic == 0x10B:      # PE32
        opt_size, dd_end = 96, 224
    elif magic == 0x20B:    # PE32+
        opt_size, dd_end = 112, 240
    else:
        return {}
    opt = read_mem(h, dll_base + pe_off + 24, dd_end)
    if not opt:
        return {}
    export_rva, _ = struct.unpack('<II', opt[opt_size:opt_size + 8])
    if not export_rva:
        return {}
    exp = read_mem(h, dll_base + export_rva, 40)
    if not exp:
        return {}
    n_names = struct.unpack('<I', exp[24:28])[0]
    funcs_rva = struct.unpack('<I', exp[28:32])[0]
    names_rva = struct.unpack('<I', exp[32:36])[0]
    ords_rva = struct.unpack('<I', exp[36:40])[0]
    found = {}
    for i in range(min(n_names, 0x10000)):
        name_rva = read_u32(h, dll_base + names_rva + i * 4)
        if name_rva is None:
            break
        name = read_cstr(h, dll_base + name_rva)
        if name in wanted:
            ord_ = read_u16(h, dll_base + ords_rva + i * 2)
            if ord_ is None:
                break
            func_rva = read_u32(h, dll_base + funcs_rva + ord_ * 4)
            if func_rva is None:
                break
            found[name] = dll_base + func_rva
    return found


# ---------------------------------------------------------------- 远程线程 stub

def shellcode_x64(ensure_addr, pyrun_addr, release_addr, script_addr):
    """x64 stub：PyGILState_Ensure -> PyRun_SimpleString(script) -> PyGILState_Release。

    远程线程是全新的 OS 线程，直接调用 Python C-API 必须先用
    PyGILState_Ensure 获取 GIL，否则 CPython 会崩溃（M0 实测踩过）。
    rbx 是 Win64 调用者保存寄存器，用于暂存 GIL state。
    """
    return (
        b'\x48\xB8' + struct.pack('<Q', ensure_addr) +    # mov rax, ensure
        b'\x48\x83\xEC\x28' +                              # sub rsp, 0x28
        b'\xFF\xD0' +                                      # call rax
        b'\x48\x89\xC3' +                                  # mov rbx, rax      (state)
        b'\x48\xB8' + struct.pack('<Q', pyrun_addr) +      # mov rax, pyrun
        b'\x48\xB9' + struct.pack('<Q', script_addr) +     # mov rcx, script
        b'\xFF\xD0' +                                      # call rax
        b'\x48\x89\xD9' +                                  # mov rcx, rbx      (state)
        b'\x48\xB8' + struct.pack('<Q', release_addr) +    # mov rax, release
        b'\xFF\xD0' +                                      # call rax
        b'\x48\x83\xC4\x28' +                              # add rsp, 0x28
        b'\x31\xC0' +                                      # xor eax, eax
        b'\xC3'                                            # ret
    )


def shellcode_x86(ensure_addr, pyrun_addr, release_addr, script_addr):
    """x86 stub（cdecl）：PyGILState_Ensure -> PyRun_SimpleString(script) -> PyGILState_Release。"""
    return (
        b'\xB8' + struct.pack('<I', ensure_addr) +         # mov eax, ensure
        b'\xFF\xD0' +                                      # call eax
        b'\x89\xC3' +                                      # mov ebx, eax      (state)
        b'\xB8' + struct.pack('<I', pyrun_addr) +          # mov eax, pyrun
        b'\x68' + struct.pack('<I', script_addr) +         # push script
        b'\xFF\xD0' +                                      # call eax
        b'\x83\xC4\x04' +                                  # add esp, 4
        b'\x53' +                                          # push ebx          (state)
        b'\xB8' + struct.pack('<I', release_addr) +        # mov eax, release
        b'\xFF\xD0' +                                      # call eax
        b'\x83\xC4\x04' +                                  # add esp, 4
        b'\x33\xC0' +                                      # xor eax, eax
        b'\xC3'                                            # ret
    )


# ---------------------------------------------------------------- 注入主流程

def inject(pid, script_text, timeout=10):
    """把 script_text 注入 pid 进程执行。返回 (ok, message)。"""
    h = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        err = ctypes.get_last_error()
        return False, 'OpenProcess 失败 (error=%d)：请以管理员身份运行，或检查进程权限' % err
    try:
        py = find_python_dll(pid)
        if not py:
            return False, '未在目标进程中找到 python DLL（该进程可能不是 Ren\'Py 游戏）'
        name, base = py
        exports = resolve_exports(h, base, [
            'PyGILState_Ensure',
            'PyRun_SimpleStringFlags',
            'PyRun_SimpleString',
            'PyGILState_Release',
        ])
        ensure = exports.get('PyGILState_Ensure')
        pyrun = exports.get('PyRun_SimpleStringFlags') or exports.get('PyRun_SimpleString')
        release = exports.get('PyGILState_Release')
        if not (ensure and pyrun and release):
            return False, '缺少所需导出 ensure=%s pyrun=%s release=%s（%s）' % (
                ensure is not None, pyrun is not None, release is not None, name)

        # 用地址高低判断目标位数（64 位进程地址远高于 4GB）
        is64 = (base >> 32) != 0 or (pyrun >> 32) != 0

        script = script_text.encode('utf-8') + b'\x00'
        saddr = kernel32.VirtualAllocEx(h, None, len(script),
                                        MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not saddr:
            return False, 'VirtualAllocEx(script) 失败'
        written = ctypes.c_size_t(0)
        if not kernel32.WriteProcessMemory(h, saddr, script, len(script), ctypes.byref(written)):
            return False, 'WriteProcessMemory(script) 失败'

        code = (shellcode_x64(ensure, pyrun, release, saddr)
                if is64 else shellcode_x86(ensure, pyrun, release, saddr))
        caddr = kernel32.VirtualAllocEx(h, None, len(code),
                                        MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
        if not caddr:
            return False, 'VirtualAllocEx(code) 失败'
        if not kernel32.WriteProcessMemory(h, caddr, code, len(code), ctypes.byref(written)):
            return False, 'WriteProcessMemory(code) 失败'

        tid = wintypes.DWORD(0)
        thread = kernel32.CreateRemoteThread(h, None, 0, caddr, None, 0, ctypes.byref(tid))
        if not thread:
            return False, 'CreateRemoteThread 失败 (error=%d)' % ctypes.get_last_error()
        kernel32.WaitForSingleObject(thread, timeout * 1000)
        kernel32.CloseHandle(thread)
        kernel32.VirtualFreeEx(h, saddr, 0, MEM_RELEASE)
        kernel32.VirtualFreeEx(h, caddr, 0, MEM_RELEASE)
        return True, '注入成功: %s @ 0x%x, PyRun_SimpleString @ 0x%x' % (name, base, pyrun)
    finally:
        kernel32.CloseHandle(h)


def main(argv):
    if len(argv) < 2:
        print('用法: python injector.py <游戏exe名或PID> [hook脚本路径]')
        print('示例: python injector.py renpy.exe')
        print('      python injector.py 12345')
        return 1
    target = argv[1]
    default_hook = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'gamehook', 'hook.py')
    hook_path = argv[2] if len(argv) > 2 else default_hook
    if not os.path.exists(hook_path):
        print('hook 脚本不存在:', hook_path)
        return 1
    with open(hook_path, 'r', encoding='utf-8') as f:
        script = f.read()
    pid = find_process(target)
    if not pid:
        print('未找到进程:', target)
        return 1
    ok, msg = inject(pid, script)
    print(msg)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
