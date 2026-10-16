"""Server-side easyScholar lookup, compatible with PaperDock rank data."""
import json
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BUNDLED_API_KEY = "efb681cf46f2432da30431e8eaaf4beb"
# easyScholar dataset abbreviations from its published appendix.  These labels
# identify the source/dataset only; rank values are preserved without inference.
DATASET_NAMES = {
    "swufe": "西南财经大学",
    "cufe": "中央财经大学",
    "uibe": "对外经济贸易大学",
    "sdufe": "山东财经大学",
    "xdu": "西安电子科技大学",
    "swjtu": "西南交通大学",
    "ruc": "中国人民大学",
    "xmu": "厦门大学",
    "sjtu": "上海交通大学",
    "fdu": "复旦大学",
    "hhu": "河海大学",
    "pku": "北大核心",
    "scu": "四川大学",
    "sciUpTop": "中科院升级版Top分区",
    "xrWarn": "新锐学术预警",
    "cqu": "重庆大学",
    "nju": "南京大学",
    "xju": "新疆大学",
    "cug": "中国地质大学",
    "ccf": "中国计算机学会",
    "cju": "长江大学（不是计量大学）",
    "zju": "浙江大学",
    "zhongguokejihexin": "中国科技核心期刊",
    "fms": "FMS",
    "utd24": "UTD24",
    "eii": "EI检索",
    "cssci": "南大核心",
    "sciUpSmall": "中科院升级版小类分区",
    "cpu": "中国药科大学",
    "xrTop": "新锐学术Top分区",
    "sciif": "SCI影响因子-JCR",
    "sci": "SCI分区-JCR",
    "ssci": "SSCI分区-JCR",
    "jci": "JCI指数-JCR",
    "sciif5": "SCI五年影响因子-JCR",
    "sciwarn": "中科院预警",
    "sciBase": "SCI基础版分区-中科院",
    "sciUp": "SCI升级版分区-中科院",
    "ajg": "ABS学术期刊指南",
    "ft50": "FT50",
    "cscd": "中国科学引文数据库",
    "ahci": "A&HCI",
    "esi": "ESI学科分类",
    "xr": "新锐学术",
    "xrSmall": "新锐学术小类分区",
}
# Ranked categories across both official and custom results.  Their order is
# intentional: international/core indexes, CAS, JCR, Xinrui, other institutions.
DATASET_GROUPS = (
    ("期刊榜单与核心目录", (
        "utd24", "ft50", "ajg", "cssci", "pku", "fms",
        "zhongguokejihexin", "cscd", "ccf", "eii", "ahci", "esi")),
    ("中科院分区", ("sciUp", "sciUpTop", "sciUpSmall", "sciBase", "sciwarn")),
    ("JCR指标与分区", ("sci", "ssci", "sciif", "sciif5", "jci")),
    ("新锐学术", ("xr", "xrTop", "xrSmall", "xrWarn")),
    ("其他高校与机构", ()),
)
DATASET_LABELS_BY_KEY = {key.casefold(): value for key, value in DATASET_NAMES.items()}
DATASET_ORDER = {
    code.casefold(): (group_index, dataset_index)
    for group_index, (_, group) in enumerate(DATASET_GROUPS)
    for dataset_index, code in enumerate(group)
}


def dataset_label(code: str, fallback: str = "") -> str:
    return DATASET_LABELS_BY_KEY.get(str(code).casefold(), fallback or str(code))


def rank_order(row: dict) -> tuple:
    code = str(row.get("abbreviation") or row["key"]).casefold()
    group, index = DATASET_ORDER.get(code, (len(DATASET_GROUPS) - 1, 1000))
    return group, index, row["name"].casefold()


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
        rows.append(dict(key=key, name=dataset_label(key), rank=rank_text(value),
                         source='Official datasets', selected=key in selected))
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
        # Prefer the appendix's official Chinese expansion when abbName is known,
        # even if the optional custom dataset metadata supplies an English name.
        if not name and abbreviation.upper() == 'DUFE':
            name = 'Dongbei University of Finance and Economics'
        name = dataset_label(abbreviation, str(name or abbreviation or 'Custom datasets'))
        label = info.get(RANK_FIELDS[int(rank) - 1]) or rank
        rows.append(dict(key=identifier, name=name, rank=rank_text(label),
                         source='Custom datasets', selected=True, abbreviation=abbreviation))
    return sorted(rows, key=rank_order)


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
    # Do not keep a process-wide lock while waiting up to 15 seconds on the
    # provider; a background list job must not block individual journal reads.
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
    with _lock:
        if len(_cache) >= 200:
            _cache.pop(next(iter(_cache)))
        _cache[cache_key] = result
    return dict(result)
