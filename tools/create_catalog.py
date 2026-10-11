"""Metadados públicos do catálogo BlueOS; sem arquivos ou medidas do ROV."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rov_history import __version__

PUBLIC = 'https://github.com/Reikjv/Bravo-Viewer'
RAW = 'https://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main'


def catalog(metadata):
    images = []
    for arch in ('arm64', 'armv7'):
        info = metadata[arch]
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', info['digest']): raise ValueError('Digest inválido')
        images.append({'expanded_size':info['size'], 'digest':info['digest'], 'platform':{'architecture':'arm' if arch == 'armv7' else arch,'variant':'v7' if arch == 'armv7' else None,'os':'linux'}})
    permissions = {'HostConfig':{'NetworkMode':'host','Memory':268435456,'NanoCpus':500000000,'Binds':['/usr/blueos/extensions/reikjv-rov-history:/data']}}
    version = {'tag':__version__,'type':'tool','images':images,
               'authors':[{'name':'Reikjv','email':'141414061+Reikjv@users.noreply.github.com'}],
               'filter_tags':['logs','history','monitoring','export'],'extra_links':{'github':PUBLIC},
               'website':PUBLIC,'readme':RAW + '/README.md','company':{'name':'Bravo Viewer','about':'Histórico e diagnóstico do ROV · Reikjv'},'permissions':permissions}
    return [{'identifier':'brs.bravo-viewer','name':'Bravo Viewer','website':PUBLIC,'docker':'ghcr.io/reikjv/bravo-viewer',
             'description':'Horas, operações, energia e ocorrências do ROV, com gráficos, inventário e exportação organizada.',
             'extension_logo':RAW + '/catalog/card.png','company_logo':RAW + '/catalog/reikjv.png','versions':{__version__:version}}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    parser.add_argument('--arch', choices=('arm64','armv7'))
    parser.add_argument('--image')
    parser.add_argument('--registry')
    parser.add_argument('--metadata-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.capture:
        info = json.loads(subprocess.check_output(['docker','image','inspect',args.registry]))[0]
        digest = next(value.split('@',1)[1] for value in info['RepoDigests'] if value.startswith('ghcr.io/reikjv/bravo-viewer@'))
        data = {'architecture':args.arch,'size':info['Size'],'digest':digest}
    else:
        data = catalog({arch:json.loads((args.metadata_dir / (arch + '.json')).read_text()) for arch in ('arm64','armv7')})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(args.output)


if __name__ == '__main__': main()
