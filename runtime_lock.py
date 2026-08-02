"""로컬 TCP 포트를 이용한 봇 단일 인스턴스 잠금과 상태 점검."""
import socket


LOCK_HOST = "127.0.0.1"
LOCK_PORT = 57391


def acquire_instance_lock() -> socket.socket:
    """잠금 포트를 선점하고 프로세스 수명 동안 유지할 소켓을 반환한다."""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind((LOCK_HOST, LOCK_PORT))
    except Exception:
        lock.close()
        raise
    return lock


def is_instance_running() -> bool:
    """잠금 포트가 이미 선점됐는지 확인한다. 상태 확인용 소켓은 즉시 닫는다."""
    try:
        lock = acquire_instance_lock()
    except OSError:
        return True
    lock.close()
    return False
