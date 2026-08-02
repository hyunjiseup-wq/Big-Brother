"""단일 인스턴스 잠금 및 로컬 실행 상태 검사."""
import unittest
from unittest.mock import patch
import socket

import runtime_lock
import runtime_status


class RuntimeLockTests(unittest.TestCase):
    def test_second_lock_is_rejected_until_first_is_closed(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((runtime_lock.LOCK_HOST, 0))
            test_port = probe.getsockname()[1]
        with patch.object(runtime_lock, "LOCK_PORT", test_port):
            first = runtime_lock.acquire_instance_lock()
            try:
                self.assertTrue(runtime_lock.is_instance_running())
            finally:
                first.close()
            self.assertFalse(runtime_lock.is_instance_running())

    def test_status_exit_codes_reflect_lock_state(self):
        with patch.object(runtime_lock, "is_instance_running", return_value=True):
            self.assertEqual(runtime_status.main(), 0)
        with patch.object(runtime_lock, "is_instance_running", return_value=False):
            self.assertEqual(runtime_status.main(), 1)


if __name__ == "__main__":
    unittest.main()
