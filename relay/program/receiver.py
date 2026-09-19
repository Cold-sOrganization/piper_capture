#!/usr/bin/env python3
"""Run on the relay: record, resumably receive, verify, then acknowledge chunks."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

from core import atomic_json, sha256


class Remote:
    def __init__(self, host='10.21.31.104', user='user', password=None):
        self.host, self.user, self.password = host, user, password
        self.client = None
        self.connect()

    def connect(self):
        import paramiko
        if self.client:
            self.client.close()
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # Unknown/mismatched hosts fail closed; establish SSH trust beforehand.
        client.connect(self.host, username=self.user, password=self.password, timeout=12,
                       look_for_keys=self.password is None, allow_agent=self.password is None)
        client.get_transport().set_keepalive(15)
        self.client = client

    def call(self, request):
        stdin, stdout, stderr = self.client.exec_command('python3 /home/user/piper_capture/control.py', timeout=50)
        stdin.write(json.dumps(request))
        stdin.channel.shutdown_write()
        text = stdout.read().decode()
        err = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        try:
            result = json.loads(text)
        except ValueError:
            raise RuntimeError('GOS collector unavailable: ' + err + text)
        if code or not result.get('ok'):
            raise RuntimeError(result.get('error', err))
        return result['result']

    def ensure_service(self):
        try:
            return self.call({'op': 'status'})
        except RuntimeError as exc:
            if 'Connection refused' not in str(exc):
                raise
        command = "cd /home/user/piper_capture && (nohup bash start_gos.sh > collector.log 2>&1 < /dev/null & echo $!)"
        _, stdout, _ = self.client.exec_command(command)
        stdout.read()
        stdout.channel.recv_exit_status()
        for _ in range(25):
            time.sleep(1)
            try:
                return self.call({'op': 'status'})
            except RuntimeError:
                pass
        raise RuntimeError('Collector did not start; inspect GOS collector.log')

    def sync(self, root, episode=None, release=True):
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        manifests = self.call({'op': 'list'})
        stats = {'chunks_downloaded': 0, 'bytes_downloaded': 0, 'episodes_complete': []}
        with self.client.open_sftp() as sftp:
            for manifest in manifests:
                eid = manifest['id']
                if episode and eid != episode:
                    continue
                if not re.fullmatch(r'[0-9]{8}_[0-9]{6}_[a-f0-9]{8}', eid):
                    raise ValueError('Unexpected remote episode ID')
                dest = root / eid
                dest.mkdir(exist_ok=True)
                for chunk in manifest['chunks']:
                    name = chunk['file']
                    if not re.fullmatch(r'chunk_[0-9]{6}\.tar', name):
                        raise ValueError('Unexpected chunk name')
                    final = dest / name
                    if not (final.exists() and final.stat().st_size == chunk['bytes'] and sha256(final) == chunk['sha256']):
                        part = dest / (name + '.part')
                        offset = part.stat().st_size if part.exists() else 0
                        if offset > chunk['bytes']:
                            part.unlink()
                            offset = 0
                        with sftp.open('/home/user/piper_capture/spool/' + eid + '/' + name, 'rb') as source:
                            source.seek(offset)
                            source.prefetch(chunk['bytes'])
                            with part.open('ab') as out:
                                while True:
                                    data = source.read(1024 * 1024)
                                    if not data:
                                        break
                                    out.write(data)
                                    stats['bytes_downloaded'] += len(data)
                                out.flush()
                                os.fsync(out.fileno())
                        if part.stat().st_size != chunk['bytes'] or sha256(part) != chunk['sha256']:
                            part.unlink()
                            raise RuntimeError('Chunk checksum mismatch; remote copy retained: ' + name)
                        os.replace(part, final)
                        stats['chunks_downloaded'] += 1
                    # Ack only after length/hash verification and fsync, including resumes.
                    self.call({'op': 'ack', 'episode': eid, 'file': name, 'sha256': chunk['sha256'], 'release': release})
                atomic_json(dest / 'manifest.json', manifest)
                if manifest['status'] != 'recording':
                    stats['episodes_complete'].append(eid)
        return stats


def main():
    import paramiko
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='10.21.31.104')
    p.add_argument('--user', default='user')
    p.add_argument('--key-auth', action='store_true', help='Use SSH agent/key instead of password prompt')
    p.add_argument('--root', default='/home/ccic/piper_datasets/raw')
    p.add_argument('--keep-gos', action='store_true', help='Retain GOS chunks after verified transfer')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('sync')
    r = sub.add_parser('record')
    r.add_argument('--task', required=True)
    r.add_argument('--duration', type=float, default=0, help='Seconds; 0 records until Ctrl+C')
    r.add_argument('--diagnostic', action='store_true', help='Allow incomplete CAN; excluded from training export')
    r.add_argument('--success', choices=['yes', 'no', 'ask'], default='ask')
    args = p.parse_args()
    password = None if args.key_auth else getpass.getpass('GOS SSH password: ')
    remote = Remote(args.host, args.user, password)
    if args.command == 'status':
        print(json.dumps(remote.ensure_service(), ensure_ascii=False, indent=2))
        return
    if args.command == 'sync':
        print(json.dumps(remote.sync(args.root, release=not args.keep_gos)))
        return
    remote.ensure_service()
    for _ in range(30):
        status = remote.call({'op': 'status'})
        if status['errors']:
            raise RuntimeError(str(status['errors']))
        if status['camera_frames'] >= 60:
            break
        time.sleep(1)
    episode = remote.call({'op': 'start', 'task': args.task, 'allow_invalid': args.diagnostic})['episode']
    print('RECORDING ' + episode + ' — Ctrl+C stops this episode', flush=True)
    started = time.monotonic()
    try:
        while not args.duration or time.monotonic() - started < args.duration:
            try:
                stats = remote.sync(args.root, episode, not args.keep_gos)
                status = remote.call({'op': 'status'})
                print(json.dumps({'elapsed_s': round(time.monotonic()-started, 1), 'transfer': stats,
                                  'sample_valid': (status['last_sample'] or {}).get('valid'), 'errors': status['errors']}), flush=True)
                if status['recording'] != episode:
                    raise RuntimeError('Collector stopped the episode; inspect manifest/error')
            except (OSError, EOFError, paramiko.SSHException) as exc:
                print('Transfer interrupted; reconnecting: ' + str(exc), flush=True)
                for attempt in range(5):
                    time.sleep(min(2 ** attempt, 8))
                    try:
                        remote.connect()
                        break
                    except (OSError, EOFError, paramiko.SSHException):
                        if attempt == 4:
                            raise RuntimeError('Cannot reconnect. GOS retains pending chunks up to its spool limit; run sync after recovery.')
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        status = remote.call({'op': 'status'})
        if status['recording'] == episode:
            success = args.success
            remote.call({'op': 'stop', 'success': None if success == 'ask' else success == 'yes'})
            if success == 'ask':
                success = 'yes' if input('Successful demonstration? [y/N] ').strip().lower() == 'y' else 'no'
                remote.call({'op': 'label', 'episode': episode, 'success': success == 'yes'})
        print(json.dumps(remote.sync(args.root, episode, not args.keep_gos)), flush=True)
        print('SAVED ' + str(Path(args.root) / episode), flush=True)


if __name__ == '__main__':
    main()
