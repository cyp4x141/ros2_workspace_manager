"""One asynchronous command with verified process-group cancellation."""

import codecs
import json
import os
from pathlib import Path
import signal
import sys
import uuid

from PyQt5.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from ..core.models import ProcessResult
from ..process_launcher import group_is_alive


class ProcessRunner(QObject):
    """Serialize commands and publish a single terminal result per command."""

    output = pyqtSignal(str, str, bool)
    completed = pyqtSignal(object)

    def __init__(self, launcher, parent=None):
        super().__init__(parent)
        self.launcher = Path(launcher)
        self._process = None
        self._task_id = None
        self._pgid = None
        self._pid = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._escalate)
        self._poll = QTimer(self)
        self._poll.setInterval(200)
        self._poll.timeout.connect(self._check_finished)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)

    @property
    def active(self):
        return self._task_id is not None

    def start(self, program, arguments, root, environment, capture=False, timeout_ms=0):
        if self.active:
            raise RuntimeError('上一项任务尚未完成收尾')
        if not self.launcher.is_file():
            raise RuntimeError(f'进程启动助手不存在: {self.launcher}')
        self._task_id = uuid.uuid4().hex
        self._pgid = None
        self._pid = None
        self._cancelled = False
        self._cleanup = False
        self._stage = 0
        self._exit = None
        self._error = ''
        self._capture = capture
        self._capture_size = 0
        self._captured = {False: [], True: []}
        self._decoders = {key: codecs.getincrementaldecoder('utf-8')('replace')
                          for key in (False, True)}
        self._buffers = {False: '', True: ''}
        process = QProcess(self)
        self._process = process
        env = QProcessEnvironment()
        for key, value in environment.items():
            env.insert(str(key), str(value))
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(root))
        process.setProgram(sys.executable)
        process.setArguments([str(self.launcher), self._task_id, program, *arguments])
        process.started.connect(lambda p=process: self._started(p))
        process.readyReadStandardOutput.connect(lambda p=process: self._read(p, False))
        process.readyReadStandardError.connect(lambda p=process: self._read(p, True))
        process.errorOccurred.connect(lambda error, p=process: self._on_error(p, error))
        process.finished.connect(lambda code, status, p=process: self._on_exit(p, code, status))
        # Return the task id before Qt can deliver any terminal signal.
        QTimer.singleShot(0, process.start)
        if timeout_ms:
            self._timeout.start(timeout_ms)
        return self._task_id

    def _started(self, process):
        if process is self._process:
            self._pid = int(process.processId())

    def _read(self, process, stderr):
        if process is not self._process or not self.active:
            return
        raw = bytes(process.readAllStandardError() if stderr else process.readAllStandardOutput())
        self._buffers[stderr] += self._decoders[stderr].decode(raw)
        while '\n' in self._buffers[stderr]:
            line, self._buffers[stderr] = self._buffers[stderr].split('\n', 1)
            self._line(line.rstrip('\r'), stderr)
        if len(self._buffers[stderr]) > 1024 * 1024:
            self._line(self._buffers[stderr], stderr)
            self._buffers[stderr] = ''

    def _line(self, line, stderr):
        prefix = '\x1eWM:' + self._task_id + ':'
        if stderr and line.startswith(prefix):
            try:
                message = json.loads(line[len(prefix):])
                if message.get('event') == 'ready' and self._pgid is None:
                    pgid = message['pgid']
                    if type(pgid) is not int or pgid != self._pid or pgid <= 1:
                        raise ValueError('启动助手报告的进程组不匹配')
                    self._pgid = pgid
                    if self._cancelled:
                        self._begin_shutdown()
                elif message.get('event') == 'launch_error':
                    self._error = '启动失败: ' + str(message.get('message', '未知错误'))
            except (ValueError, TypeError, KeyError) as exc:
                self._error = f'启动握手失败: {exc}'
                self.cancel()
            return
        if self._capture:
            self._capture_size += len(line)
            if self._capture_size > 8 * 1024 * 1024:
                self._error = 'colcon 查询输出超过上限，请检查工作空间'
                self.cancel()
                return
            self._captured[stderr].append(line)
        self.output.emit(self._task_id, line, stderr)

    def _on_error(self, process, error):
        if process is not self._process or not self.active:
            return
        if error == QProcess.FailedToStart:
            self._error = '启动失败: ' + process.errorString()
            self._exit = (-1, False)
            self._finish()
        elif error != QProcess.Crashed:
            self._error = process.errorString()

    def _on_exit(self, process, code, status):
        if process is not self._process or not self.active:
            return
        for stderr in (False, True):
            self._read(process, stderr)
            self._buffers[stderr] += self._decoders[stderr].decode(b'', final=True)
            if self._buffers[stderr]:
                self._line(self._buffers[stderr], stderr)
                self._buffers[stderr] = ''
        self._exit = (code, status == QProcess.NormalExit)
        self._timeout.stop()
        if self._pgid is None and not self._error:
            self._error = '启动助手未确认进程组，命令未被确认启动'
        self._check_finished()
        if self.active:
            self._poll.start()

    def cancel(self):
        if not self.active or self._cancelled:
            return
        self._cancelled = True
        self._timeout.stop()
        if self._pgid is not None:
            self._begin_shutdown()
        else:
            # Wait briefly for the setsid handshake rather than signalling the GUI's group.
            self._timer.start(3000)

    def _begin_shutdown(self):
        if self._stage:
            return
        self._stage = 1
        self._send(signal.SIGINT)
        self._timer.start(3000)

    def _send(self, sig):
        if self._pgid is None:
            return
        try:
            os.killpg(self._pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            self._error = f'无法停止本任务进程组: {exc}'

    def _escalate(self):
        if not self.active:
            return
        if self._pgid is None:
            self._error = self._error or '启动握手超时'
            self._process.kill()
            return
        if self._stage < 2:
            self._stage = 2
            self._send(signal.SIGTERM)
            self._timer.start(2000)
        else:
            self._stage = 3
            self._send(signal.SIGKILL)
            self._poll.start()
        self._check_finished()

    def _timed_out(self):
        if self.active:
            self._error = 'colcon 查询超时'
            self.cancel()

    def _check_finished(self):
        if not self.active or self._exit is None:
            return
        try:
            alive = self._pgid is not None and group_is_alive(self._pgid)
        except OSError as exc:
            message = f'无法确认子进程是否已退出，继续保留操作锁: {exc}'
            if message != self._error:
                self.output.emit(self._task_id, message, True)
            self._error = message
            return
        if alive:
            if not self._cancelled and not self._cleanup:
                self._cleanup = True
                self._error = self._error or '主进程退出时仍有子进程，已执行残留清理'
                self._begin_shutdown()
            return
        self._finish()

    def _finish(self):
        if not self.active:
            return
        self._timer.stop()
        self._poll.stop()
        self._timeout.stop()
        result = ProcessResult(
            self._task_id, self._exit[0], self._exit[1], self._cancelled,
            '\n'.join(self._captured[False]), '\n'.join(self._captured[True]), self._error)
        process = self._process
        self._process = None
        self._task_id = None
        self._pgid = None
        process.deleteLater()
        self.completed.emit(result)
