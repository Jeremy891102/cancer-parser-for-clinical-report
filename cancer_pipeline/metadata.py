"""Source metadata retain identifier strings and normalize explicit missing sentinels."""


def normalize_metadata(meta):
    return {
        k: (
            None
            if v is None
            or isinstance(v, str)
            and v.strip().lower() in {"", "null", "none", "nan", "n/a", "na", "[mrn]"}
            else v
        )
        for k, v in meta.items()
    }
