import paramiko
from queue import Queue
from threading import Lock
import time


class SFTPConnectionPool:
    def __init__(self, host, port, username, password, max_connections=5):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.max_connections = max_connections
        self._pool = Queue(maxsize=max_connections)
        self._lock = Lock()
        self._active_connections = 0

        # 初始化连接池
        for _ in range(min(2, max_connections)):
            self._pool.put(self._create_new_connection())

    def _create_new_connection(self):
        """创建新的SFTP连接"""
        transport = paramiko.Transport((self.host, self.port))
        transport.connect(username=self.username, password=self.password)
        self._active_connections += 1
        return transport

    def _is_connection_healthy(self, transport):
        """检查连接是否健康"""
        if not transport or not transport.is_active():
            return False

        try:
            # 简单测试连接是否可用
            sftp = paramiko.SFTPClient.from_transport(transport)
            sftp.listdir('.')
            sftp.close()
            return True
        except:
            return False

    def get_connection(self, timeout=10):
        """改进版获取连接，包含健康检查"""
        start_time = time.time()

        while time.time() - start_time < timeout:
            # 首先尝试从池中获取
            if not self._pool.empty():
                transport = self._pool.get_nowait()
                if self._is_connection_healthy(transport):
                    return transport
                else:
                    self._close_connection(transport)

            # 如果还能创建新连接
            with self._lock:
                if self._active_connections < self.max_connections:
                    return self._create_new_connection()

            # 短暂等待后重试
            time.sleep(0.1)

        raise Exception("Connection pool exhausted or timeout")

    def return_connection(self, transport):
        """将连接返回池中"""
        if transport and transport.is_active():
            self._pool.put(transport)
        else:
            self._close_connection(transport)

    def _close_connection(self, transport):
        """关闭连接"""
        if transport:
            try:
                if transport.is_active():
                    transport.close()
            finally:
                self._active_connections -= 1

    def close_all(self):
        """关闭所有连接"""
        while not self._pool.empty():
            transport = self._pool.get_nowait()
            self._close_connection(transport)

    def __del__(self):
        self.close_all()