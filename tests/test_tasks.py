import hashlib

import pytest

from greyqueue.tasks import execute, validate


@pytest.mark.parametrize(
    "task,args",
    [
        ("shell", {"command": "whoami"}),
        ("sleep", {"seconds": -1.0}),
        ("sleep", {"seconds": 6.0}),
        ("calculate_pi", {"iterations": True}),
        ("hash_text", {"text": "x", "path": "secret"}),
        ("hash_text", {"text": "x" * 10001}),
    ],
)
def test_invalid_tasks(task, args):
    with pytest.raises(ValueError):
        validate(task, args)


def test_registered_results():
    assert execute("hash_text", {"text": "abc"}) == {"sha256": hashlib.sha256(b"abc").hexdigest()}
    assert abs(execute("calculate_pi", {"iterations": 10000})["pi"] - 3.14159) < 0.001
    assert execute("sleep", {"seconds": 0.0}) == {"slept_seconds": 0.0}
