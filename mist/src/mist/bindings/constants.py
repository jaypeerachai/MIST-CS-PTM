"""Terms and source roots used by binding analysis."""

from __future__ import annotations

from mist.resources import DATA_DIR
from mist.rules.binding_terms import build_binding_term_config


SOURCE_ROOT_DIRS = {
    "lib",
    "python",
    "src",
    "source",
}


TERM_CONFIG = build_binding_term_config(DATA_DIR / "reuse_rules.xlsx")


MODEL_ARG_NAMES = TERM_CONFIG.model_arg_names


MODEL_PAYLOAD_KEYS = TERM_CONFIG.model_payload_keys


GENERIC_HTTP_ORIGINS = {"httpx", "requests", "urllib"}


HTTP_MODEL_ENDPOINT_TERMS = {
    "chat",
    "completion",
    "completions",
    "embedding",
    "embeddings",
    "generatecontent",
    "generate_content",
    "responses",
}


HTTP_WEAK_ENDPOINT_TERMS = {
    "model",
    "models",
}


FRAMEWORK_CONFIG_EDGE_TYPES = {
    "framework_selector_choice_set",
    "framework_form_value_persisted",
    "framework_state_read",
    "outer_scope_value_to_free_variable",
}
