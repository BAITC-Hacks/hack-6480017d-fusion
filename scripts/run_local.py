#!/usr/bin/env python3
"""Start both local Fusion servers, or check them without changing any process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
HOST = '127.0.0.1'
BACKEND_PORT = 8000
FRONTEND_PORT = 5173
URL = f'http://{HOST}:{FRONTEND_PORT}'
STARTUP_SECONDS = 25


def port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.4):
            return True
    except OSError:
        return False


def get(port: int, path: str) -> bytes:
    request = urllib.request.Request(f'http://{HOST}:{port}{path}')
    # The development server is local; never forward these checks via a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=1) as response:
        if response.status != 200:
            raise ValueError('HTTP response is not successful')
        return response.read(1_000_000)


def readiness() -> tuple[bool, str]:
    for port, name in ((BACKEND_PORT, 'Backend'), (FRONTEND_PORT, 'Интерфейс')):
        if not port_open(port):
            return False, f'{name}: порт {port} не отвечает.'
    try:
        health = json.loads(get(BACKEND_PORT, '/api/health'))
        if health.get('status') != 'ok' or health.get('service') != 'Fusion':
            return False, 'На порту 8000 отвечает другое приложение или Fusion не готов.'
        html = get(FRONTEND_PORT, '/').decode('utf-8')
        if not re.search(r'<title>\s*Fusion\b', html):
            return False, 'На порту 5173 не найден интерфейс Fusion.'
        proxy_health = json.loads(get(FRONTEND_PORT, '/api/health'))
        if proxy_health.get('status') != 'ok' or proxy_health.get('service') != 'Fusion':
            return False, 'Интерфейс не соединяется с backend через /api.'
        analysis = json.loads(get(FRONTEND_PORT, '/api/analysis'))
        if not isinstance(analysis.get('nodes'), int) or not isinstance(analysis.get('top_nodes'), list):
            return False, 'Сервер отвечает, но результаты расчёта ещё недоступны.'
    except (OSError, urllib.error.URLError, ValueError, AttributeError):
        return False, 'Fusion ещё не готов: проверьте запуск, локальные данные и сообщения серверов.'
    return True, f'Fusion готов: {URL}'


def stop_children(children: list[subprocess.Popen]) -> None:
    # Only groups created by this launcher are signalled. Existing servers are
    # detected before Popen and are never added to this list.
    for child in children:
        try:
            if os.name == 'posix':
                os.killpg(child.pid, signal.SIGTERM)
            elif child.poll() is None:
                child.terminate()
        except ProcessLookupError:
            pass
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                if os.name == 'posix':
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Запуск backend и интерфейса Fusion одной командой. Существующие процессы не останавливаются.',
        epilog='Из корня проекта: .venv/bin/python scripts/run_local.py. Ctrl+C завершает только процессы этого запуска.',
    )
    parser.add_argument('--check', action='store_true', help='Только проверить готовность серверов, proxy и расчёта; ничего не запускать и не останавливать.')
    args = parser.parse_args()
    occupied = [port for port in (BACKEND_PORT, FRONTEND_PORT) if port_open(port)]
    if args.check or occupied:
        ready, message = readiness()
        print(message, flush=True)
        if ready:
            if not args.check:
                print('Используйте уже запущенное приложение. Серверы оставлены включёнными.', flush=True)
            return 0
        if not args.check:
            print(f'Заняты порты: {", ".join(map(str, occupied))}. Второй экземпляр не запущен.', flush=True)
            print('Проверьте открытые терминалы. Для общего запуска освободите эти порты, остановив только свои серверы через Ctrl+C, затем повторите команду. Чужие процессы не останавливайте.', flush=True)
        return 1

    npm = shutil.which('npm')
    if npm is None:
        print('Не найден npm. Установите Node.js и зависимости frontend по README.', file=sys.stderr)
        return 1
    if not (ROOT / 'frontend' / 'node_modules').is_dir():
        print('Не установлены зависимости интерфейса: выполните npm --prefix frontend ci.', file=sys.stderr)
        return 1
    commands = [
        [sys.executable, '-m', 'uvicorn', 'backend.app:app', '--host', HOST, '--port', str(BACKEND_PORT)],
        [npm, '--prefix', 'frontend', 'run', 'dev'],
    ]
    children: list[subprocess.Popen] = []
    try:
        print('Запускаем Fusion. Логи обоих серверов выводятся в этом терминале.', flush=True)
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, start_new_session=os.name == 'posix'))
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                print('Один из серверов завершился. Причина указана в его сообщениях выше.', file=sys.stderr)
                return 1
            ready, message = readiness()
            if ready:
                print(f'\n{message}\nОставьте терминал открытым. Для остановки этого запуска: Ctrl+C.', flush=True)
                break
            time.sleep(0.4)
        else:
            print(f'Серверы не стали готовы за {STARTUP_SECONDS} секунд. {message}', file=sys.stderr)
            return 1
        while True:
            if any(child.poll() is not None for child in children):
                print('Один из серверов остановился; завершаем второй процесс этого запуска.', file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print('\nОстанавливаем процессы, запущенные этой командой…', flush=True)
        return 0
    except OSError:
        print('Не удалось запустить серверы. Проверьте окружение Python и Node.js по README.', file=sys.stderr)
        return 1
    finally:
        stop_children(children)


if __name__ == '__main__':
    raise SystemExit(main())
