from __future__ import annotations

from hashlib import sha256

from toolang.plugin.catalogs.models_dev.parsing import model_catalog_snapshot_from_data
from toolang.setup.cache_environment import environment_fingerprint, environment_names


def test_model_environment_names_collect_provider_and_model_api_templates() -> None:
    snapshot = model_catalog_snapshot_from_data(
        {
            "cloud": {
                "id": "cloud",
                "name": "Cloud",
                "npm": "@ai-sdk/openai-compatible",
                "api": "https://${CLOUD_HOST}/v1/$$literal/$REGION",
                "env": ["CLOUD_TOKEN", "ACCOUNT_ID"],
                "models": {
                    "one": {
                        "id": "one",
                        "name": "One",
                        "modalities": {},
                        "limit": {},
                        "provider": {"api": "https://$MODEL_HOST/${ACCOUNT_ID}/v1"},
                    }
                },
            }
        },
        revision="test",
    )

    assert environment_names(snapshot) == (
        "ACCOUNT_ID",
        "CLOUD_HOST",
        "CLOUD_TOKEN",
        "MODEL_HOST",
        "REGION",
    )


def test_environment_fingerprint_is_ordered_hashed_and_omits_unset_names() -> None:
    values = {"B": "two", "A": "one"}

    fingerprint = environment_fingerprint(("B", "MISSING", "A", "B"), values)

    assert fingerprint == (
        ("A", sha256(b"one").hexdigest()),
        ("B", sha256(b"two").hexdigest()),
    )
    assert "one" not in repr(fingerprint)
    assert "two" not in repr(fingerprint)
