"""Check rendered role/ownership boundaries; this does not replace API-server or CSI tests."""

import argparse
import re
from pathlib import Path

import yaml


def validate(path: Path, placeholders: bool) -> None:
    objects = list(yaml.safe_load_all(path.read_text()))
    workloads = [o for o in objects if o['kind'] in {'StatefulSet', 'Deployment'}]
    assert workloads, 'no workloads rendered'
    for workload in workloads:
        spec = workload['spec']
        assert spec['replicas'] in (0, 1), 'identity cannot be scaled'
        if workload['kind'] == 'StatefulSet':
            assert spec['updateStrategy']['type'] == 'OnDelete'
        template = spec['template']
        pod = template['spec']
        role = template['metadata']['labels']['flowtool-role']
        assert pod['automountServiceAccountToken'] is False
        assert not pod.get('hostPID') and not pod.get('hostNetwork')
        assert pod['securityContext']['runAsNonRoot'] is True
        assert pod['terminationGracePeriodSeconds'] >= 45
        for volume in pod.get('volumes', []):
            assert 'hostPath' not in volume, 'host filesystem must not be mounted'
        for container in pod['containers'] + pod.get('initContainers', []):
            digest = container['image'].split('@sha256:')
            assert len(digest) == 2 and re.fullmatch('[0-9a-f]{64}', digest[1]), 'image must use digest'
            assert placeholders or digest[1] != '0' * 64, 'replace placeholder digest'
            assert container['securityContext']['allowPrivilegeEscalation'] is False
        secrets = {v['secret']['secretName'] for v in pod.get('volumes', []) if 'secret' in v}
        secrets |= {e['secretRef']['name'] for c in pod['containers'] for e in c.get('envFrom', []) if 'secretRef' in e}
        if role.startswith('worker-'):
            assert secrets == {f'flowtool-{role}-env', 'flowtool-worker-ca'}, 'worker secret boundary changed'
            for claim in spec['volumeClaimTemplates']:
                assert claim['spec']['accessModes'] == ['ReadWriteOncePod']
                assert placeholders or not claim['spec']['storageClassName'].startswith('replace-')
        elif role == 'serve':
            assert secrets == {'flowtool-serve-env', 'flowtool-app-key', 'flowtool-worker-tls'}
            serve = next(c for c in pod['containers'] if c['name'] == 'serve')
            assert 'livenessProbe' not in serve and 'startupProbe' not in serve, 'DB stalls must not restart serve'
            assert serve['readinessProbe']['httpGet']['path'] == '/readyz'
        elif role == 'cloudflare':
            assert secrets == {'flowtool-tunnel-credentials'}
    for resource in objects:
        if resource['kind'] == 'Service':
            assert resource['spec'].get('type', 'ClusterIP') == 'ClusterIP', 'no public service'
        if resource['kind'] == 'ConfigMap' and 'config.yml' in resource.get('data', {}):
            rules = yaml.safe_load(resource['data']['config.yml'])['ingress']
            assert len(rules) == 2 and rules[0]['path'] == '^/webhook$'
            assert rules[1] == {'service': 'http_status:404'}, 'tunnel must default deny'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rendered', type=Path)
    parser.add_argument('--allow-placeholders', action='store_true')
    args = parser.parse_args()
    validate(args.rendered, args.allow_placeholders)
    print('Rendered role/ownership checks passed; cluster behavior has not been verified.')


if __name__ == '__main__':
    main()
