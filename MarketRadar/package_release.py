"""Create an allowlisted source checkout for publishing, without local state.

Run from the parent project: python -m MarketRadar.package_release
The release directory can have its own Git history; this script never deletes it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'MarketRadar' / '.release'


def source_files():
    files = [ROOT / 'models.py', ROOT / 'subagents' / 'amazon_market_agent_v2.py',
             ROOT / 'subagents' / 'listing_agent_v2.py', ROOT / 'subagents' / '__init__.py']
    for directory, pattern in [('MarketRadar', '*.py'), ('MarketRadar/tests', '*.py'),
                               ('MarketRadar/static', '*'), ('MarketRadar/integrations', '*.md'),
                               ('prompts/amazon_market_research', '*.md'), ('MarketRadar/deploy', '*')]:
        files.extend(path for path in (ROOT / directory).glob(pattern) if path.is_file())
    files.extend(ROOT / 'MarketRadar' / name for name in ('README.md', 'requirements.txt', 'start.ps1'))
    return sorted(set(files))


def package(output=DEFAULT):
    output = Path(output).resolve()
    if output in (ROOT, ROOT / 'MarketRadar') or ROOT in output.parents and '.release' not in output.parts:
        raise ValueError('Use a dedicated .release folder or a directory outside the working project.')
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for path in source_files():
        relative = path.relative_to(ROOT)
        data = path.read_bytes()
        text = data.decode('utf-8')
        # Fail before publication on recognizable credentials, rather than print them.
        if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{30,})', text):
            raise ValueError(f'Credential-shaped literal found in {relative}; publication stopped.')
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest()})
    (output / 'README.md').write_text((ROOT / 'MarketRadar' / 'README.md').read_text(encoding='utf-8')
                                     .replace('](integrations/', '](MarketRadar/integrations/')
                                     .replace('](deploy/', '](MarketRadar/deploy/'), encoding='utf-8')
    (output / '.gitignore').write_text('.env\n.env.*\n*.pyc\n__pycache__/\n.venv/\nnode_modules/\n**/.data/\n**/.release/\n.playwright*/\nrelease-manifest.json\n', encoding='utf-8')
    (output / '.dockerignore').write_text('**/.git\n**/.env*\n**/.data\n**/.release\n**/__pycache__\n**/node_modules\n**/.venv\n**/.playwright*\n', encoding='utf-8')
    workflow = output / 'MarketRadar/deploy/ci-smoke.yml'
    if workflow.exists():
        destination = output / '.github/workflows/container.yml'
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workflow, destination)
    (output / 'release-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'directory': str(output), 'files': len(manifest), 'state': 'packaged; not deployed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT)
    print(json.dumps(package(parser.parse_args().output)))
