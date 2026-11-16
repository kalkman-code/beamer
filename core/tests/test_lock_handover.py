import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from core import receiver


class LockedMachine:
    def is_locked(self):
        return True


def responder(handover):
    instance = receiver.LinkResponder.__new__(receiver.LinkResponder)
    link = SimpleNamespace(peer_id=b"owner-peer-id!!!", name="Owner")
    instance._owner = link
    instance._unlock = LockedMachine()
    instance._unlocking = threading.Event()
    instance._arriving_visit = None
    instance._lock_checked_at = float("-inf")
    instance._locked_since = None
    instance._sent_home_for_lock = False
    instance._secure_handover = handover
    instance._send_home = mock.Mock(return_value=True)
    return instance, link


def two_checks(instance, link):
    with mock.patch.object(receiver.time, "monotonic", side_effect=(10.0, 11.0)):
        instance._home_if_locked(link)
        instance._home_if_locked(link)


class LockHome(unittest.TestCase):
    def test_without_the_secure_path_the_owner_goes_home_after_two_checks(self):
        instance, link = responder(None)
        two_checks(instance, link)
        instance._send_home.assert_called_once_with(link)

    def test_an_unapproved_peer_goes_home_after_two_checks(self):
        handover = mock.Mock(return_value=False)
        instance, link = responder(handover)
        two_checks(instance, link)
        handover.assert_called_with(link.peer_id)
        instance._send_home.assert_called_once_with(link)

    def test_an_approved_peer_stays_while_the_service_runs(self):
        handover = mock.Mock(return_value=True)
        instance, link = responder(handover)
        two_checks(instance, link)
        handover.assert_called_with(link.peer_id)
        instance._send_home.assert_not_called()

    def test_a_failing_check_counts_as_unapproved(self):
        instance, link = responder(mock.Mock(side_effect=OSError("no registry")))
        two_checks(instance, link)
        instance._send_home.assert_called_once_with(link)

    def test_a_non_true_answer_counts_as_unapproved(self):
        instance, link = responder(mock.Mock(return_value="yes"))
        two_checks(instance, link)
        instance._send_home.assert_called_once_with(link)


if __name__ == "__main__":
    unittest.main()
