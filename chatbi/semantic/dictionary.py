"""jieba 自定义词典：从语义快照生成词条文件并加载，提供带 nature 的分词。

词条文件（data/dict/custom_dict.txt）格式：`词 词频 简码`。
注意：jieba 0.42 用户词典的 tag 只接受小写字母（re: `[a-z]+`），因此第三列只能放
小写简码；完整 nature（如 `_dimval_dim_region:HD`）另存 data/dict/word_nature.json，
load 时读入内存，segment() 输出时以映射表还原完整 nature。

nature 编码约定（借鉴 SuperSonic DictWord）：
  _term_term_net_sales        术语（name+aliases）
  _dim_dim_store              维度（display_name+aliases）
  _dimval_dim_region:HD       维度值（tech_value/display_value/query_aliases 全部入库）
  _entity_ent_customer        实体
  _attr_attr_store_region     属性
  _dataset_ds_sales           数据集
  _model_mdl_orders           模型
  _custom                     手工补充词条（dict_entry 表）
"""

import json
import sqlite3

import jieba
import jieba.posseg

from chatbi.config import DICT_DIR, DICT_FILE, ensure_dirs
from chatbi.semantic.store import SemanticSnapshot

_FREQ_NAME = 50000   # 正式名称
_FREQ_VALUE = 40000  # 维度值展示名
_FREQ_ALIAS = 30000  # 别名
_FREQ_VALUE_ALIAS = 20000  # 维度值查询别名/技术值

# 完整 nature 前缀 → jieba 合法小写简码
_NATURE_SHORT = {
    "_term_": "term",
    "_dimval_": "dimval",
    "_dim_": "dim",
    "_entity_": "entity",
    "_attr_": "attr",
    "_dataset_": "dataset",
    "_model_": "model",
    "_custom": "custom",
}

_WORD_NATURE_FILE = DICT_DIR / "word_nature.json"

_word_nature: dict[str, str] = {}
_loaded_file: str | None = None


def _short_code(nature: str) -> str:
    for prefix, code in _NATURE_SHORT.items():
        if nature.startswith(prefix):
            return code
    return "custom"


def collect_entries(snap: SemanticSnapshot, conn: sqlite3.Connection | None = None) -> list[tuple[str, int, str]]:
    """从快照（+dict_entry 手工词条）收集全部词典词条：(词, 词频, 完整nature)。"""
    entries: list[tuple[str, int, str]] = []

    def add(word: str | None, freq: int, nature: str) -> None:
        word = (word or "").strip()
        if word:
            entries.append((word, freq, nature))

    for t in snap.terms.values():
        add(t["name"], _FREQ_NAME, f"_term_{t['id']}")
        for a in t.get("aliases", []):
            add(a, _FREQ_ALIAS, f"_term_{t['id']}")

    for e in snap.entities.values():
        add(e["name"], _FREQ_NAME, f"_entity_{e['id']}")
        for a in e.get("aliases", []):
            add(a, _FREQ_ALIAS, f"_entity_{e['id']}")

    for a in snap.attributes.values():
        add(a["name"], _FREQ_NAME, f"_attr_{a['id']}")
        for al in a.get("aliases", []):
            add(al, _FREQ_ALIAS, f"_attr_{a['id']}")

    for d in snap.dimensions.values():
        add(d["display_name"], _FREQ_NAME, f"_dim_{d['id']}")
        for a in d.get("aliases", []):
            add(a, _FREQ_ALIAS, f"_dim_{d['id']}")
        for vm in d.get("value_maps", []):
            nature = f"_dimval_{d['id']}:{vm['tech_value']}"
            add(vm.get("display_value"), _FREQ_VALUE, nature)
            add(vm.get("tech_value"), _FREQ_VALUE_ALIAS, nature)
            for qa in vm.get("query_aliases", []):
                add(qa, _FREQ_VALUE_ALIAS, nature)

    for ds in snap.datasets.values():
        add(ds["name"], _FREQ_NAME, f"_dataset_{ds['id']}")
    for mo in snap.models.values():
        add(mo["name"], _FREQ_NAME, f"_model_{mo['id']}")

    if conn is not None:
        for row in conn.execute("SELECT word, nature, freq FROM dict_entry"):
            add(row["word"], row["freq"], row["nature"])

    # 同词多归属时保留词频最高的一条（jieba 一个词只取一条词性；完整冲突由 linking 阶段处理）
    best: dict[str, tuple[int, str]] = {}
    for word, freq, nature in entries:
        if word not in best or freq > best[word][0]:
            best[word] = (freq, nature)
    return [(w, f, n) for w, (f, n) in sorted(best.items())]


def build_dictionary(snap: SemanticSnapshot, conn: sqlite3.Connection | None = None) -> int:
    """全量重建词典文件与 nature 映射，返回词条数。"""
    ensure_dirs()
    entries = collect_entries(snap, conn)
    with open(DICT_FILE, "w", encoding="utf-8") as f:
        for word, freq, nature in entries:
            f.write(f"{word} {freq} {_short_code(nature)}\n")
    with open(_WORD_NATURE_FILE, "w", encoding="utf-8") as f:
        json.dump({w: n for w, _, n in entries}, f, ensure_ascii=False, indent=0)
    return len(entries)


def load_dictionary() -> bool:
    """把词典文件热加载进 jieba，并读入 word→nature 映射（文件不存在时返回 False）。"""
    global _loaded_file, _word_nature
    if not DICT_FILE.exists():
        return False
    jieba.load_userdict(str(DICT_FILE))
    if _WORD_NATURE_FILE.exists():
        with open(_WORD_NATURE_FILE, encoding="utf-8") as f:
            _word_nature = json.load(f)
    _loaded_file = str(DICT_FILE)
    return True


def segment(text: str) -> list[tuple[str, str]]:
    """分词并返回 [(词, nature)]；命中自定义词典的词输出完整 nature
    （如 `_dimval_dim_region:HD`），未覆盖的词保留 jieba 默认词性。"""
    if _loaded_file != str(DICT_FILE):
        load_dictionary()
    return [
        (pair.word, _word_nature.get(pair.word, pair.flag))
        for pair in jieba.posseg.cut(text)
    ]
