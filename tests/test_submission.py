import pytest

from tasks._submission import UncertainSubmission


def test_uncertain_submission_is_an_exception():
    assert issubclass(UncertainSubmission, Exception)


def test_carries_markers():
    err = UncertainSubmission("step3_stuck_on_firma", markers={"idBorr": "531503"})
    assert err.markers == {"idBorr": "531503"}
    assert "step3" in str(err)


def test_markers_default_to_empty_dict():
    err = UncertainSubmission("reg lost visibility")
    assert err.markers == {}
