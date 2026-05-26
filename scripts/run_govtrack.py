import os
import subprocess
import sys
import time


def start_email_watcher(env):
    """
    Starts the email watcher service in a separate process.

    Returns:
        subprocess.Popen: Running watcher process.
    """
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "govtrack.services.Email_watcher"],
        env=env,
    )


def start_streamlit_app(env):
    """
    Starts the Streamlit dashboard application.

    Returns:
        subprocess.Popen: Running Streamlit process.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "govtrack/ui/streamlit_app.py",
        ],
        env=env,
    )


def stop_process(process, timeout=5):
    """
    Gracefully stops a running process.

    Args:
        process (subprocess.Popen): Process to stop.
        timeout (int): Time to wait before force killing.
    """
    if process.poll() is None:  # Process is still running
        process.terminate()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()


def main():
    """
    Main entry point of the application.

    - Sets required environment variables.
    - Starts email watcher service.
    - Starts Streamlit UI.
    - Keeps program alive while Streamlit is running.
    - Cleans up background processes on exit.
    """

    # Copy current environment variables
    env = os.environ.copy()

    # Ensure UTF-8 encoding for subprocess output
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # Start background email watcher service
    watcher_process = start_email_watcher(env)

    try:
        # Start Streamlit application
        streamlit_process = start_streamlit_app(env)

        # Keep script alive while Streamlit is running
        while streamlit_process.poll() is None:
            time.sleep(1)

    finally:
        # Ensure watcher service is stopped properly
        stop_process(watcher_process)


if __name__ == "__main__":
    main()