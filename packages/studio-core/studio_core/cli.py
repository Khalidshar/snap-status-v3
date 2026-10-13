import argparse
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from studio_core import __version__
from studio_core.config import Config
from studio_core.network.egress import EgressServer
from studio_core.jobs.gateway import CommandGateway
from studio_core.jobs import Repository
from studio_core.network import NetworkClient, NetworkError
from studio_core.observability import Redactor
from studio_core.storage import Storage


def demo(config, secret):
    repository = Repository(config, Redactor([secret]))
    gateway = CommandGateway(config, repository, secret)
    body = {'channel': 'scheduler', 'actor': config.owners['scheduler'][0], 'event_id': uuid.uuid4().hex,
            'command': 'demo', 'priority': 'NORMAL'}
    now = time.time()
    job, created = gateway.accept(body, now, gateway.signature(body, now))
    duplicate, duplicate_created = gateway.accept(body, now, gateway.signature(body, now))
    assert created and not duplicate_created and duplicate['id'] == job['id']
    claim = repository.claim()
    if claim is None or claim['id'] != job['id']:
        raise RuntimeError('Demo requires an empty foundation queue')
    storage = Storage(config)
    workspace = storage.workspace(job['id'])
    source = NetworkClient(config).get('https://core.telegram.org/bots/api')
    (workspace / 'input' / 'fixture.json').write_text(json.dumps({'synthetic': True}), encoding='utf-8')
    phases = ['RESEARCHING','PROCESSING','DOWNLOADING','DESIGNING','RENDERING',
              'VALIDATING','REPAIRING','VALIDATING','DELIVERING','COMPLETED']
    for state in phases:
        repository.transition(job['id'], claim['lease_token'], state, {'synthetic': True})
    storage.manifest(job['id'], {'job_id': job['id'], 'app': config.app, 'core_version': __version__,
                                'synthetic': True, 'assets': [], 'network_fixture_sha256': source.sha256,
                                'external_delivery': False})
    repository.backup(config.data_root / 'foundation-backup.sqlite')
    return {'app': config.app, 'core_version': __version__, 'job_id': job['id'], 'state': repository.get(job['id'])['state'],
            'timeline': [e['event'] if e['event'] != 'job.state' else json.loads(e['details'])['state'] for e in repository.timeline(job['id'])],
            'idempotency_passed': True, 'fixture_http_status': source.status, 'external_delivery': False}


def probe(config):
    denied = {}
    for name, family, kind in [('ipv4_tcp',socket.AF_INET,socket.SOCK_STREAM),
                               ('ipv6_tcp',socket.AF_INET6,socket.SOCK_STREAM),
                               ('ipv4_udp',socket.AF_INET,socket.SOCK_DGRAM)]:
        try:
            with socket.socket(family, kind):
                denied[name] = False
        except OSError as exc:
            denied[name] = exc.errno in (errno.EAFNOSUPPORT, errno.EPERM, errno.EACCES)
    script = "import socket,sys\ntry:\n socket.socket(socket.AF_INET,socket.SOCK_STREAM)\nexcept OSError as e:\n sys.exit(0 if e.errno in (97,1,13) else 2)\nelse:\n sys.exit(1)"
    denied['child_ipv4'] = subprocess.run([sys.executable, '-c', script], timeout=5).returncode == 0
    response = NetworkClient(config).get('https://core.telegram.org/bots/api')
    try:
        NetworkClient(config).get('https://example.com/')
        denied['non_allowlisted_host'] = False
    except NetworkError as exc:
        denied['non_allowlisted_host'] = str(exc) == 'HOST_NOT_ALLOWED'
    result = {'app': config.app, 'checks': denied, 'allowed_https_status': response.status,
              'allowed_https_sha256': response.sha256, 'passed': all(denied.values()) and response.status == 200}
    if not result['passed']:
        raise RuntimeError('Egress enforcement proof failed: ' + json.dumps(result))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=['proxy','demo','probe'])
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.action == 'proxy':
        with EgressServer(config) as server:
            server.serve_forever()
        return
    if args.action == 'demo':
        result = demo(config, os.environ['STUDIO_GATEWAY_SECRET'])
    else:
        result = probe(config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
