"""외부 API나 Discord 로그인 없이 빅브라더 실행 상태만 확인한다."""
import runtime_lock


def main() -> int:
    if runtime_lock.is_instance_running():
        print("[RUNNING] Big Brother instance lock is active.")
        return 0
    print("[STOPPED] Big Brother instance lock is not active.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
