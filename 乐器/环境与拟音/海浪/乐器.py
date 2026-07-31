from tianlai.procedural_sfx import create_procedural_sfx


def create(*, manifest, sample_rate, base_directory):
    return create_procedural_sfx(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
