"""Rebuild the self-contained HTML app after editing scenarios.json or site_template.html."""
from pathlib import Path
import json
folder=Path(__file__).resolve().parent
scenarios=json.loads((folder/'scenarios.json').read_text(encoding='utf-8'))
if len(scenarios)<1:raise ValueError('No scenarios found')
html=(folder/'site_template.html').read_text(encoding='utf-8').replace('__SCENARIOS__',json.dumps(scenarios,ensure_ascii=False,separators=(',',':')).replace('</','<\\/'))
(folder/'index.html').write_text(html,encoding='utf-8')
print(f'Rebuilt index.html with {len(scenarios)} scenarios.')
