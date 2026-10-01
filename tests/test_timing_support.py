from __future__ import annotations

import sys

import pytest

from timing_support import supervised_process


def test_supervisor_preserves_failure_output_and_exit_status():
    result = supervised_process(
        [sys.executable, "-c", "print('negative assertion'); raise SystemExit(7)"],
        timeout=5,
    )
    assert result.returncode == 7
    assert result.stdout == "negative assertion\n"


def test_supervisor_times_out_hung_process_without_waiting_for_thread_shutdown():
    with pytest.raises(AssertionError, match="watchdog"):
        supervised_process(
            [sys.executable, "-c", "import threading; threading.Event().wait()"],
            timeout=0.1,
        )


def test_supervisor_reclaims_a_ready_descendant_process_group(tmp_path, monkeypatch):
    import json
    import os
    import selectors
    import signal
    import subprocess
    import time
    import timing_support

    child = "import os,signal,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); os.write(int(sys.argv[1]),b'R'); signal.pause()"
    code = (
        "import json,os,signal,subprocess,sys\n"
        "read_fd,write_fd=os.pipe()\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r},str(write_fd)],pass_fds=(write_fd,))\n"
        "os.close(write_fd)\nassert os.read(read_fd,1)==b'R'\n"
        "os.write(sys.stdout.fileno(),(json.dumps({'child_pid':child.pid})+'\\n').encode())\n"
        "signal.pause()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5), "descendant did not acknowledge startup"
            ready = json.loads(os.read(process.stdout.fileno(), 4096))
        child_pid = ready["child_pid"]
        assert os.getpgid(child_pid) == process.pid

        class SubprocessProxy:
            # Reuse the phase-proven process so startup cannot satisfy the
            # timeout branch without ever having created a descendant.
            @staticmethod
            def Popen(*args, **kwargs):
                return process

            def __getattr__(self, name):
                return getattr(subprocess, name)

        monkeypatch.setattr(timing_support, "subprocess", SubprocessProxy())
        with pytest.raises(AssertionError, match="watchdog"):
            supervised_process(["phase-proven-fixture"], timeout=0.1)
        assert process.returncode == -signal.SIGKILL
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, "descendant remains after supervisor cleanup"
            time.sleep(0.001)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()
