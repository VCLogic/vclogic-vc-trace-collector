from vc_trace_collector.download_backoff import DownloadBackoff


def test_transient_failures_back_off_then_pause():
    policy = DownloadBackoff()
    assert policy.record("HTTP Error 429: Too Many Requests") == 60
    assert policy.record("HTTP Error 429: Too Many Requests") == 120
    assert policy.record("Sign in to confirm you’re not a bot") == 240
    assert policy.record("Connection timed out") is None


def test_success_resets_backoff_and_unavailable_does_not_retry():
    policy = DownloadBackoff()
    assert policy.record("Connection timed out") == 60
    assert policy.record(None) == 5
    assert policy.record("Video unavailable. This video is private") == 5
    assert policy.record("HTTP Error 503") == 60


def test_unknown_errors_eventually_pause():
    policy = DownloadBackoff()
    for _ in range(3):
        assert policy.record("unclassified downloader failure") is not None
    assert policy.record("unclassified downloader failure") is None
