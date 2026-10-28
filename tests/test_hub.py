import json
import unittest

from perseus.hub import HubError, RepoFiles, inspect
from perseus.parsers import ParserError, pick
from tests.models import deepseek_v3, llama31_8b, qwen3_235b

QWEN_CONFIG = {"architectures": ["Qwen3ForCausalLM"], "model_type": "qwen3", "num_hidden_layers": 64,
               "num_attention_heads": 64, "num_key_value_heads": 8, "head_dim": 128, "hidden_size": 5120,
               "max_position_embeddings": 40960, "torch_dtype": "bfloat16"}
TEMPLATE = "{% if tools %}<tools>{% endif %}{{ '<think>' }}"


class FakeHub:
    def __init__(self, sizes, texts, params=None):
        self.sizes, self.texts, self.params = sizes, texts, params or {"BF16": 32_762_123_264}

    def files(self, repo, revision):
        return RepoFiles("f" * 40, dict(self.sizes), False, sum(self.params.values()), self.params)

    def read(self, repo, sha, name):
        if name not in self.texts:
            raise HubError(f"missing {name}")
        t = self.texts[name]
        return t if isinstance(t, str) else json.dumps(t)


def qwen_repo(**over):
    sizes = {"config.json": 700, "tokenizer.json": 11_000_000, "tokenizer_config.json": 9_000,
             "generation_config.json": 200, "model.safetensors.index.json": 50_000,
             "model-00001-of-00002.safetensors": 32_000_000_000,
             "model-00002-of-00002.safetensors": 33_524_246_528,
             "pytorch_model.bin": 65_000_000_000, "original/consolidated.pth": 65_000_000_000,
             "README.md": 5000, ".gitattributes": 1500}
    texts = {"config.json": QWEN_CONFIG,
             "tokenizer_config.json": {"chat_template": TEMPLATE},
             "model.safetensors.index.json": {"weight_map": {"a": "model-00001-of-00002.safetensors",
                                                             "b": "model-00002-of-00002.safetensors"}}}
    sizes.update(over.pop("sizes", {}))
    texts.update(over.pop("texts", {}))
    return FakeHub(sizes, texts, **over)


class Inspect(unittest.TestCase):
    def test_downloads_only_what_vllm_loads(self):
        insp = inspect(qwen_repo(), "Qwen/Qwen3-32B", "main", False)
        self.assertNotIn("pytorch_model.bin", insp.download)
        self.assertNotIn("original/consolidated.pth", insp.download)
        self.assertNotIn("README.md", insp.download)
        self.assertIn("model-00002-of-00002.safetensors", insp.download)
        self.assertIn("tokenizer.json", insp.download)
        m = insp.meta
        self.assertEqual(m.weight_bytes, 65_524_246_528)
        self.assertEqual((m.num_layers, m.num_kv_heads, m.head_dim, m.max_positions), (64, 8, 128, 40960))
        self.assertEqual(m.native_dtype, "bf16")
        self.assertTrue(m.thinks)
        self.assertEqual(m.revision_sha, "f" * 40)

    def test_remote_code_refused_unless_trusted(self):
        repo = qwen_repo(sizes={"modeling_x.py": 4000},
                         texts={"config.json": {**QWEN_CONFIG, "auto_map": {"AutoModel": "modeling_x.X"}}})
        with self.assertRaises(HubError) as e:
            inspect(repo, "x/y", "main", False)
        self.assertIn("modeling_x.py", str(e.exception))
        insp = inspect(repo, "x/y", "0" * 40, True)
        self.assertIn("modeling_x.py", insp.download)

    def test_no_safetensors(self):
        repo = FakeHub({"config.json": 1, "pytorch_model.bin": 10}, {"config.json": QWEN_CONFIG})
        with self.assertRaises(HubError) as e:
            inspect(repo, "x/y", "main", False)
        self.assertIn("bin", str(e.exception))

    def test_no_chat_template(self):
        repo = qwen_repo(texts={"tokenizer_config.json": {}})
        with self.assertRaises(HubError):
            inspect(repo, "x/y", "main", False)

    def test_awq_and_nested_text_config(self):
        cfg = {"architectures": ["Mistral3ForConditionalGeneration"], "model_type": "mistral3",
               "text_config": {**QWEN_CONFIG, "model_type": "mistral"},
               "quantization_config": {"quant_method": "awq", "bits": 4}}
        insp = inspect(qwen_repo(texts={"config.json": cfg}), "x/y-AWQ", "main", False)
        self.assertEqual((insp.meta.quant_method, insp.meta.native_dtype), ("awq", "int4"))
        self.assertEqual(insp.meta.model_type, "mistral")

    def test_compressed_tensors_fp8(self):
        q = {"quant_method": "compressed-tensors",
             "config_groups": {"g0": {"weights": {"num_bits": 8, "type": "float"}}}}
        insp = inspect(qwen_repo(texts={"config.json": {**QWEN_CONFIG, "quantization_config": q}}), "x/y", "main", False)
        self.assertEqual(insp.meta.quant_method, "fp8")

    def test_template_file_preferred(self):
        repo = qwen_repo(sizes={"chat_template.jinja": 100}, texts={"chat_template.jinja": "plain {{ messages }}",
                                                                     "tokenizer_config.json": {}})
        insp = inspect(repo, "x/y", "main", False)
        self.assertFalse(insp.meta.thinks)
        self.assertTrue(any("never mentions tools" in w for w in insp.warnings))


class Parsers(unittest.TestCase):
    def test_families(self):
        self.assertEqual(pick(qwen3_235b()), ("hermes", "qwen3"))
        self.assertEqual(pick(llama31_8b()), ("llama3_json", None))
        self.assertEqual(pick(deepseek_v3()), ("deepseek_v3", None))  # template doesn't think
        coder = qwen3_235b()
        coder.repo_id, coder.thinks = "Qwen/Qwen3-Coder-480B-A35B-Instruct", False
        self.assertEqual(pick(coder), ("qwen3_coder", None))

    def test_overrides(self):
        self.assertEqual(pick(qwen3_235b(), "pythonic", "none"), ("pythonic", None))
        unknown = llama31_8b()
        unknown.repo_id, unknown.model_type, unknown.architectures = "acme/thing", "thing", ["ThingLM"]
        with self.assertRaises(ParserError):
            pick(unknown)
        self.assertEqual(pick(unknown, "hermes"), ("hermes", None))


if __name__ == "__main__":
    unittest.main()
