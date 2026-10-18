import logging
import subprocess
import threading
import select

logger = logging.getLogger(__name__)

# Even on Linux the stderr/stdout of child processes is often silenced or not accessible,
# this enables a capture of the combined stderr/stdout output to the status window, via the logging facilities
# similar to Popen.communicate, but with threads
class LinuxCapturedPopenFactory:
    def __init__(self):
        self._enabled = True
        self.fds = []
        self.threads = []

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        self._enabled = v

    def on_shutdown_signal(self, **kwargs):
        # Closing when we exit since we won't be polling anymore, to avoid blocking if the pipes fill up
        if len(self.fds) == 0:
            return
        logger.info(f'LinuxCapturedPopenFactory closing {len(self.fds)} child process output streams.')
        for f in self.fds:
            f.close()
        self.fds = []

    def set_shutdown_signal(self, s):
        s.connect(self.on_shutdown_signal)

    def _thread_read(self, f):
        try:
            while True:
                ready, _, _ = select.select([f], [], [], 1.0)

                if ready:
                    l = f.readline()
                    if not l:  # EOF
                        break
                    logger.info(l.strip('\n'))
        except ValueError as _:
            logger.info('Child process stdout read error in CapturedPopenFactory thread, likely due to EOF.')
        finally:
            try:
                self.fds.remove(f)
            except ValueError:
                # we're in a race with on_shutdown_signal, ignore this
                pass

    def Popen(self, cmd, cwd=None):
        if not self.enabled:
            return subprocess.Popen(
                cmd,
                cwd=cwd,
            )

        p = subprocess.Popen(
            cmd,
            cwd=cwd,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.fds.append(p.stdout)
        t = threading.Thread(target=self._thread_read, args=(p.stdout, ), daemon=True).start()
        self.threads.append(t)
        return p
