"""DPAPI for the existing one-time /claim credential in a durable Windows inbox.

No key files, environment changes, plaintext logging, or external dependencies.
Only the same Windows account can recover a binding command after a crash.
"""
import base64
import ctypes
from ctypes import wintypes
import os


class Blob(ctypes.Structure):
    _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, *, decrypt):
    if os.name!='nt':raise RuntimeError('windows_dpapi_required_for_binding')
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    result=Blob()
    library=ctypes.WinDLL('crypt32',use_last_error=True)
    method=library.CryptUnprotectData if decrypt else library.CryptProtectData
    method.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
        ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    method.restype=wintypes.BOOL
    if not method(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result)):
        raise RuntimeError('binding_protection_failed')
    free=ctypes.WinDLL('kernel32').LocalFree
    free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
    try:return ctypes.string_at(result.data,result.size)
    finally:free(result.data)


def protect(text):return base64.b64encode(_crypt(text.encode('utf-8'),decrypt=False)).decode('ascii')
def unprotect(ciphertext):return _crypt(base64.b64decode(ciphertext,validate=True),decrypt=True).decode('utf-8')
