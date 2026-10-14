"""Server-side easyScholar lookup, compatible with PaperDock rank data."""
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BUNDLED_API_KEY = "efb681cf46f2432da30431e8eaaf4beb"
DATASET_NAMES = {'sciUp': 'CAS upgraded division', 'sciUpSmall': 'CAS subject division', 'sciUpTop': 'CAS Top', 'sciBase': 'CAS basic division', 'sciwarn': 'CAS warning list', 'sciif': 'JCR impact factor', 'sciif5': 'JCR five-year impact factor', 'sci': 'SCI JCR quartile', 'ssci': 'SSCI JCR quartile', 'jci': 'JCR JCI', 'ajg': 'Academic Journal Guide (ABS)', 'utd24': 'UTD24', 'ft50': 'FT50', 'fms': 'FMS', 'ccf': 'China Computer Federation', 'eii': 'EI', 'pku': 'Peking University Core', 'cssci': 'CSSCI', 'cscd': 'CSCD', 'ahci': 'A&HCI', 'esi': 'ESI', 'swufe': 'Southwestern University of Finance and Economics', 'cqu': 'Chongqing University', 'cufe': 'Central University of Finance and Economics', 'nju': 'Nanjing University', 'uibe': 'University of International Business and Economics', 'xju': 'Xinjiang University', 'sdufe': 'Shandong University of Finance and Economics', 'cug': 'China University of Geosciences', 'xdu': 'Xidian University', 'swjtu': 'Southwest Jiaotong University', 'cju': 'Yangtze University', 'ruc': 'Renmin University of China', 'zju': 'Zhejiang University', 'xmu': 'Xiamen University', 'zhongguokejihexin': 'Chinese Science and Technology Core Journals', 'sjtu': 'Shanghai Jiao Tong University', 'fdu': 'Fudan University', 'hhu': 'Hohai University', 'scu': 'Sichuan University', 'cpu': 'China Pharmaceutical University', 'xr': 'Xinrui Academic', 'xrWarn': 'Xinrui warning list', 'xrTop': 'Xinrui Top', 'xrSmall': 'Xinrui subject division'}
PRIORITY = ('sciUp', 'sciUpSmall', 'sciUpTop', 'sciBase', 'sciwarn', 'sciif',
            'sciif5', 'sci', 'ssci', 'jci', 'ajg', 'utd24', 'ft50', 'fms')
RANK_FIELDS = ('oneRankText', 'twoRankText', 'threeRankText', 'fourRankText', 'fiveRankText')


def rank_text(value):
    if isinstance(value, (list, tuple)):
        return '; '.join(rank_text(item) for item in value)
    if isinstance(value, dict):
        return '; '.join('%s: %s' % (key, rank_text(item)) for key, item in value.items())
    return str(value)


def rank_rows(data):
    official = data.get('officialRank') or {}
    all_ranks = official.get('all') or {}
    selected = official.get('select') or {}
    rows = []
    for key, value in all_ranks.items():
        if value is None or value == '' or value == [] or value == {}:
            continue
        rows.append(dict(key=key, name=DATASET_NAMES.get(key, key), rank=rank_text(value),
                         source='Official datasets', selected=key in selected))
    rows.sort(key=lambda row: (PRIORITY.index(row['key']) if row['key'] in PRIORITY else len(PRIORITY), row['name']))
    custom = data.get('customRank') or {}
    infos = custom.get('rankInfo') or []
    if isinstance(infos, dict):
        infos = list(infos.values()) if 'uuid' not in infos else [infos]
    datasets = {str(info.get('uuid')): info for info in infos if isinstance(info, dict)}
    ranks = custom.get('rank') or []
    if isinstance(ranks, str):
        ranks = [ranks]
    for value in ranks:
        identifier, separator, rank = str(value).partition('&&&')
        if not separator or rank not in ('1', '2', '3', '4', '5') or identifier not in datasets:
            continue
        info = datasets[identifier]
        abbreviation = str(info.get('abbName') or '').strip()
        name = info.get('name') or info.get('fullName') or info.get('rankName')
        if not name:
            name = DATASET_NAMES.get(abbreviation, DATASET_NAMES.get(abbreviation.lower(), abbreviation))
            if abbreviation.upper() == 'DUFE':
                name = 'Dongbei University of Finance and Economics'
        label = info.get(RANK_FIELDS[int(rank) - 1]) or rank
        rows.append(dict(key=identifier, name=str(name or 'Custom datasets'), rank=rank_text(label),
                         source='Custom datasets', selected=True, abbreviation=abbreviation))
    return rows


_cache = {}
_lock = threading.Lock()


def lookup(name: str, refresh: bool = False) -> dict:
    name = " ".join(name.split())
    if not name or len(name) > 300 or any(ord(char) < 32 for char in name):
        raise ValueError("Choose a valid journal name.")
    key = os.environ.get("EASYSCHOLAR_API_KEY", "").strip() or BUNDLED_API_KEY
    cache_key = (name.casefold(), key)
    with _lock:
        cached = _cache.get(cache_key)
        if not refresh and cached and time.time() - cached["queried_at"] < 86400:
            return dict(cached, cached=True)
        params = urlencode(dict(secretKey=key, publicationName=name))
        request = Request("https://www.easyscholar.cc/open/getPublicationRank?" + params,
                          headers={"Accept": "application/json", "User-Agent": "NetVault"})
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise ValueError(f"easyScholar returned HTTP {exc.code}. Please try again later.") from None
        except (URLError, OSError):
            raise ValueError("Unable to reach easyScholar. Please try again later.") from None
        except (ValueError, UnicodeError):
            raise ValueError("Invalid response from easyScholar.") from None
        if not isinstance(payload, dict):
            raise ValueError("Invalid response from easyScholar.")
        if str(payload.get("code")) != "200":
            raise ValueError("easyScholar could not complete the lookup. Please try again later.")
        data = payload.get("data") or {}
        try:
            rows = rank_rows(data)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Invalid rank data from easyScholar.") from None
        result = dict(journal_name=name, rows=rows, queried_at=time.time(), cached=False)
        if len(_cache) >= 200:
            _cache.pop(next(iter(_cache)))
        _cache[cache_key] = result
        return dict(result)
