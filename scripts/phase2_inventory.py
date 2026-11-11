"""Source-only Phase II inventory; никогда не обходит private/runtime storage."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = Path('/home/igor/coliseum_build_checkpoints/phase2_prebuild')
NEW_SOURCE = (
    'services/archive_worker.py', 'services/export_jobs.py', 'utils/private_files.py',
    'utils/chat_content.py', 'tests/test_arc01_archive_worker.py', 'tests/test_dispatcher_phase2.py',
    'tests/test_export_phase2.py', 'tests/test_phase2_measurements.py',
    'requirements-test-phase2.txt', 'scripts/phase2_inventory.py',
)


def safe_source(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or any(part in {
        '.git', '.venv', 'backups', 'storage', 'imgs', '__pycache__',
    } for part in rel.parts):
        raise ValueError('Запрещённый путь в source inventory')
    if rel.name.startswith(('.env', '.coverage')) or rel.suffix not in {'.py', '.md', '.txt', '.ini', ''}:
        raise ValueError('Не source-файл')
    for index in range(1, len(rel.parts)+1):
        if (root / Path(*rel.parts[:index])).is_symlink():
            raise ValueError('Символьная ссылка в source inventory')
    return root/rel


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if ROOT != Path('/home/igor/study_bot_audit'):
        raise RuntimeError('Inventory разрешён только в audit workspace')
    manifest = CHECKPOINT/'CHECKPOINT_MANIFEST.txt'
    baseline = {}
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)  \((\d+) bytes\)', line)
        if match:
            sha, relative, size = match.groups()
            path = safe_source(CHECKPOINT, relative)
            if digest(path) != sha or path.stat().st_size != int(size):
                raise RuntimeError('Checkpoint checksum mismatch')
            baseline[relative] = sha
    if len(baseline) != 114:
        raise RuntimeError('Неожиданный состав Phase I checkpoint')
    changed = []
    for relative in sorted(set(baseline) | set(NEW_SOURCE)):
        path = safe_source(ROOT, relative)
        sha = digest(path)
        if sha != baseline.get(relative):
            changed.append(dict(path=relative, phase1_sha256=baseline.get(relative), phase2_sha256=sha,
                                bytes=path.stat().st_size, feature='new' if relative not in baseline else 'modified'))
    if digest(ROOT/'BUILD_REPORT.md') != baseline['BUILD_REPORT.md']:
        raise RuntimeError('Phase I report был изменён')
    result = dict(checkpoint=str(CHECKPOINT), checkpoint_manifest_sha256=digest(manifest),
                  verified_checkpoint_files=len(baseline), changed_source=changed, phase1_report_unchanged=True)
    (ROOT/'PHASE2_SOURCE_MANIFEST.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')
    print(f'Checkpoint verified: {len(baseline)}; Phase II source changes: {len(changed)}')


if __name__ == '__main__':
    main()
