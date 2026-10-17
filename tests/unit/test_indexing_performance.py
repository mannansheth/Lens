"""Generated dependency trees must not become application evidence."""
from lens.repository.indexing import index, scan


def test_scan_prunes_named_and_detected_python_environments(tmp_path):
    for name in ('VENV', '.venv', 'virtualenv', 'site-packages', '__pypackages__', 'NODE_MODULES', 'dist', 'build'):
        path = tmp_path / name
        path.mkdir()
        (path / 'third_party.py').write_text('def dependency():\n    pass\n')
    detected = tmp_path / 'custom-python-environment'
    detected.mkdir()
    (detected / 'pyvenv.cfg').write_text('home = placeholder')
    (detected / 'third_party.py').write_text('def dependency():\n    pass\n')
    application = tmp_path / 'backend-py'
    application.mkdir()
    (application / 'service.py').write_text('def service():\n    pass\n')
    knowledge, _ = scan(tmp_path)
    assert [file.path for file in knowledge.files] == ['backend-py/service.py']
    assert knowledge.statistics['directories_ignored'] == 9


def test_env_directory_without_virtual_environment_marker_is_preserved(tmp_path):
    path = tmp_path / 'env'
    path.mkdir()
    (path / 'configuration.py').write_text('def configure():\n    pass\n')
    knowledge, _ = scan(tmp_path)
    assert [file.path for file in knowledge.files] == ['env/configuration.py']


def test_pipeline_revision_change_reuses_unchanged_embedding_text(tmp_path):
    from lens.database.store import PIPELINE_REVISION

    (tmp_path / 'sample.py').write_text('def answer():\n    return 42\n')

    class Store:
        def __init__(self):
            self.project = None
            self.hashes = {}
            self.cached = {}

        def snapshot(self, project):
            return {'project': self.project, 'hashes': self.hashes}

        def cached_embeddings(self, project, model, dimension):
            return self.cached

        def replace(self, root, knowledge, chunks, embeddings, model, dimension):
            self.project = {'model': model, 'dimension': dimension,
                            'pipeline_revision': PIPELINE_REVISION}
            self.hashes = {file.path: file.hash for file in knowledge.files}
            self.cached = {chunk.metadata['embedding_hash']: embedding.vector
                           for chunk, embedding in zip(chunks, embeddings, strict=True)}

    class Provider:
        model = 'fixture'
        dimension = 2

        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    store, provider = Store(), Provider()
    index(tmp_path, store, provider)
    first_embedding_count = len(provider.calls[0])
    assert first_embedding_count > 0

    # A parser/relationship-only revision forces a rebuild, but the exact
    # embedding texts still have reusable vectors for this model and dimension.
    store.project['pipeline_revision'] = 'older-revision'
    report = index(tmp_path, store, provider)
    assert report['status'] == 'indexed'
    assert report['embeddings_generated'] == 0
    assert report['embeddings_reused'] == first_embedding_count
    assert len(provider.calls) == 1
