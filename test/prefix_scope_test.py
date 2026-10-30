from unittest.mock import patch
import pytest
from git_remote_s3 import S3Remote, UriScheme


@patch("boto3.Session.client")
def test_all_root_listings_keep_repository_separator(client):
    client.return_value.list_objects_v2.return_value = {
        "NextContinuationToken": "page2"
    }
    remote = S3Remote(UriScheme.S3, None, "bucket", "repos/p1")
    client.return_value.list_objects_v2.side_effect = [
        {"NextContinuationToken": "page2"},
        {"Contents": []},
    ]
    remote.list_refs(bucket="bucket", prefix="repos/p1")
    assert all(
        call.kwargs["Prefix"] == "repos/p1/"
        for call in client.return_value.list_objects_v2.call_args_list
    )


@patch("boto3.Session.client")
def test_fetch_worker_failure_is_propagated(client):
    remote = S3Remote(UriScheme.S3, None, "bucket", "repos/p1")
    with patch.object(remote, "cmd_fetch", side_effect=RuntimeError("failed download")):
        with pytest.raises(RuntimeError, match="failed download"):
            remote.process_fetch_cmds(["fetch abc refs/heads/main"])
