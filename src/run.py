from eventlet.tpool import socket
from flask import Flask, session, Response
import zipfile
#import eventlet
#eventlet.monkey_patch()  # 必须在所有其他导入之前调用
from urllib.parse import quote
import io
import os,json
import paramiko
from flask import render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO
from tqdm import tqdm
from threading import Lock
from flask_cors import CORS
from flask import send_file
from contextlib import contextmanager
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

from .sftp_pool import SFTPConnectionPool


app = Flask(__name__)


CORS(app)  # 添加在 Flask 应用初始化后
app.config['SECRET_KEY'] = 'secret!'

# 配置文件路径
CONFIG_FILE = 'ssh_configs.json'



# 初始化连接池
sftp_pool = None

# 建议配置
POOL_CONFIG = {
    'max_connections': 5,  # 根据服务器性能调整
    'connection_timeout': 10,  # 获取连接超时(秒)
    'health_check_interval': 300  # 健康检查间隔(秒)
}
# 初始化配置文件
if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w') as f:
        json.dump({}, f)

def load_configs():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except json.decoder.JSONDecodeError:
        print("JSONDecodeError: No valid JSON object could be decoded from the string.")
        return {}

def save_configs(configs):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(configs, f, indent=4)

@app.route('/manage-ssh', methods=['GET', 'POST'])
def manage_ssh_configs():
    if request.method == 'POST':
        config_name = request.form.get('config_name')
        config = {
            'hostname': request.form.get('hostname'),
            'port': int(request.form.get('port', 22)),
            'username': request.form.get('username'),
            'password': request.form.get('password'),
            'allow_agent': request.form.get('allow_agent') == 'on',
            'look_for_keys': request.form.get('look_for_keys') == 'on'
        }

        configs = load_configs()
        configs[config_name] = config
        save_configs(configs)

        return jsonify({'success': True})

    configs = load_configs()
    return render_template('manage_ssh.html', configs=configs)


@app.route('/')
def index():
    global sftp_pool
    if sftp_pool:
        sftp_pool.close_all()
    if 'current_config' in session:
        session.clear()
    return redirect(url_for('manage_ssh_configs'))

@app.route('/select-config/<config_name>')
def select_config(config_name):
    configs = load_configs()
    if config_name in configs:
        session['current_config'] = configs[config_name]
        return redirect(url_for('file_browser'))
    return redirect(url_for('manage_ssh_configs'))

@app.route('/delete-config', methods=['POST'])
def delete_config():
    config_name = request.form.get('config_name')
    configs = load_configs()
    if config_name in configs:
        del configs[config_name]
        save_configs(configs)
    return jsonify({'success': True})

@app.route('/pool-status')
def pool_status():
    global sftp_pool
    return jsonify({
        'active_connections': sftp_pool._active_connections,
        'idle_connections': sftp_pool._pool.qsize(),
        'max_connections': sftp_pool.max_connections
    })

@contextmanager
def sftp_session():
    """提供SFTP会话的上下文管理器"""
    global sftp_pool
    config = session['current_config']
    # 这里添加连接服务器并获取文件列表的逻辑
    global sftp_pool
    if sftp_pool:
        sftp_pool.close_all()
    sftp_pool = SFTPConnectionPool(
        host=config['hostname'],
        port=config['port'],
        username=config['username'],
        password=config['password'],
        max_connections=10
    )
    sftp = None
    transport = None
    try:
        transport = sftp_pool.get_connection()
        sftp = paramiko.SFTPClient.from_transport(transport)
        yield sftp
    finally:
        if sftp:
            sftp.close()
        if transport:
            sftp_pool.return_connection(transport)

def get_sftp_client():
    """从池中获取SFTP客户端"""
    global sftp_pool
    transport = sftp_pool.get_connection()
    return paramiko.SFTPClient.from_transport(transport), transport

def release_sftp_client(sftp, transport):
    """释放SFTP客户端回连接池"""
    global sftp_pool
    try:
        if sftp:
            sftp.close()
        sftp_pool.return_connection(transport)
    except Exception as e:
        print(f"Error releasing connection: {str(e)}")
        sftp_pool._close_connection(transport)


@app.route('/download_items', methods=['POST'])
def download_items_files():
    sftp = None
    transport = None
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        file_paths = data.get('files', [])

        if not file_paths:
            return jsonify({'success': False, 'error': 'No files selected'}), 400

        # Get connection from pool
        sftp, transport = get_sftp_client()
        transport.sock.settimeout(30)  # Set socket timeout

        def generate():
            try:
                for file_path in file_paths:
                    file_path = file_path.replace('\\', '/')
                    print(file_path)

                    try:
                        # Verify connection is still active
                        if not transport.is_active():
                            yield "Connection lost\r\n"
                            return

                        # Get file info
                        try:
                            file_attr = sftp.stat(file_path)
                            filename = os.path.basename(file_path)
                            file_size = file_attr.st_size
                        except Exception as e:
                            yield f"Error accessing {file_path}: {str(e)}\r\n"
                            continue

                        # File header
                        yield f"Content-Disposition: attachment; filename=\"{filename}\"\r\n"
                        yield f"Content-Type: application/octet-stream\r\n"
                        yield f"Content-Length: {file_size}\r\n\r\n"

                        # Stream file content
                        try:
                            with sftp.open(file_path, 'rb') as remote_file:
                                chunk_size = 1024 * 1024  # 1MB chunks
                                bytes_sent = 0

                                while True:
                                    try:
                                        chunk = remote_file.read(chunk_size)
                                        print(len(chunk))
                                        if not chunk:
                                            break
                                        bytes_sent += len(chunk)
                                        yield chunk
                                    except socket.timeout:
                                        yield "\r\n\r\nTransfer timeout\r\n"
                                        return
                                    except Exception:
                                        yield "\r\n\r\nTransfer interrupted\r\n"
                                        return

                        except Exception as e:
                            yield f"\r\n\r\nError reading {file_path}: {str(e)}\r\n"
                            continue

                        # File separator
                        yield "\r\n\r\n---NEXT-FILE---\r\n\r\n"

                    except Exception as e:
                        yield f"\r\n\r\nError processing {file_path}: {str(e)}\r\n"
                        continue

            finally:
                # Ensure connection is released
                if sftp or transport:
                    release_sftp_client(sftp, transport)

        response = Response(
            generate(),
            mimetype='multipart/mixed',
            headers={
                'Cache-Control': 'no-cache',
                'Transfer-Encoding': 'chunked',
                'X-Accel-Buffering': 'no'  # Important for streaming in some servers
            }
        )
        return response

    except Exception as e:
        if sftp or transport:
            release_sftp_client(sftp, transport)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/download', methods=['POST'])
def download_files():
    sftp = None
    transport = None
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type必须为application/json'})

        data = request.get_json()
        file_paths = data.get('files', [])

        if not file_paths:
            return jsonify({'success': False, 'error': '未选择文件'})

        # 从连接池获取连接
        sftp, transport = get_sftp_client()

        # 创建内存中的ZIP文件
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in file_paths:
                file_path = file_path.replace('\\', '/')
                try:
                    # 获取文件信息
                    file_attr = sftp.stat(file_path)

                    # 使用分块读取
                    with sftp.open(file_path, 'rb') as remote_file:
                        chunk_size = 1024 * 1024  # 1MB chunks
                        file_data = io.BytesIO()

                        while True:
                            chunk = remote_file.read(chunk_size)
                            if not chunk:
                                break
                            file_data.write(chunk)

                        file_data.seek(0)
                        filename = os.path.basename(file_path)

                        # 添加到ZIP
                        zipf.writestr(
                            zinfo_or_arcname=filename,
                            data=file_data.getvalue(),
                            compress_type=zipfile.ZIP_DEFLATED
                        )

                except Exception as e:
                    print(f"Error processing {file_path}: {str(e)}")
                    continue

        zip_buffer.seek(0)

        response = send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name='downloaded_files.zip'
        )

        response.headers['Content-Length'] = zip_buffer.getbuffer().nbytes
        return response

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if sftp or transport:
            release_sftp_client(sftp, transport)

# 新增：获取文件大小总和
@app.route('/get_files_size', methods=['POST'])
def get_files_size():
    if not request.is_json:
        return jsonify({'success': False, 'error': 'Content-Type必须为application/json'})

    try:
        data = request.get_json()
        file_paths = data.get('files', [])
        total_size = 0

        sftp = get_sftp_connection()
        for file_path in file_paths:
            file_path = file_path.replace('\\', '/')
            try:
                total_size += sftp.stat(file_path).st_size
            except:
                continue

        sftp.close()
        return jsonify({'success': True, 'total_size': total_size})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

def get_sftp_connection():
    """建立SFTP连接"""
    if 'current_config' not in session:
        return redirect(url_for('manage_ssh_configs'))

    config = session['current_config']

    # 示例数据：
    transport = paramiko.Transport((config['hostname'], config['port']))
    transport.connect(
        username=config['username'],
        password=config['password']
    )
    return paramiko.SFTPClient.from_transport(transport)

def validate_linux_path(path):
    """验证Linux路径是否合法"""
    if not isinstance(path, str):
        return False
    if '../' in path or '/..' in path:
        return False  # 防止目录遍历攻击
    if '\0' in path:
        return False  # 防止空字节攻击
    return True


def list_directory(path='/'):
    """列出目录内容"""
    try:
        # 规范化路径 - 替换所有反斜杠为正斜杠
        path = path.replace('\\', '/')

        # 确保路径以斜杠开头
        if not path.startswith('/'):
            path = '/' + path

        sftp = get_sftp_connection()
        files = []

        for item in sftp.listdir_attr(path):
            # 使用正斜杠拼接路径
            full_path = f"{path.rstrip('/')}/{item.filename}"
            files.append({
                'name': item.filename,
                'path': full_path,
                'is_dir': item.st_mode & 0o40000 != 0,
                'size': item.st_size,
                'modified': item.st_mtime
            })

        sftp.close()
        return {'success': True, 'files': files}
    except IOError as e:
        return {'success': False, 'error': f"无法访问路径: {path}"}
    except Exception as e:
        return {'success': False, 'error': str(e)}


@app.route('/file-browser')
def file_browser():
    if 'current_config' not in session:
        return redirect(url_for('manage_ssh_configs'))
    config = session['current_config']
    context = {"config": config}
    return render_template('browser.html', **context)


# 使用示例
@app.route('/list', methods=['POST'])
def list_files():
    try:
        if not request.is_json:
            return jsonify({'success': False, 'error': 'Content-Type必须为application/json'})

        data = request.get_json()
        path = data.get('path', '/').replace('\\', '/').rstrip('/') or '/'

        with sftp_session() as sftp:
            files = []
            for item in sftp.listdir_attr(path):
                full_path = f"{path}/{item.filename}"
                files.append({
                    'name': item.filename,
                    'path': full_path,
                    'is_dir': item.st_mode & 0o40000 != 0,
                    'size': item.st_size,
                    'modified': item.st_mtime
                })

            return jsonify({'success': True, 'files': files, 'current_path': path})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == "__main__":
    app.run(app, debug=True, port=5003)