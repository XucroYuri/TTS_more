from fastapi.testclient import TestClient

from app.main import create_app


def test_stale_library_save_preserves_imported_roles(tmp_path):
    client = TestClient(create_app(data_root=tmp_path))
    old = client.get('/api/characters')
    assert old.status_code == 200
    assert old.headers['etag']
    imported = [{'id':'imported', 'name':'Imported', 'profiles':[]}]
    assert client.put('/api/characters', json=imported).status_code == 200
    stale = client.put('/api/characters', json=[], headers={'If-Match':old.headers['etag']})
    assert stale.status_code == 412
    assert [c['id'] for c in client.get('/api/characters').json()] == ['imported']


def test_current_library_revision_allows_save_and_changes_etag(tmp_path):
    client = TestClient(create_app(data_root=tmp_path))
    revision = client.get('/api/characters').headers['etag']
    saved = client.put('/api/characters', json=[{'id':'role','name':'Role','profiles':[]}], headers={'If-Match':revision})
    assert saved.status_code == 200
    assert saved.headers['etag'] != revision
    assert client.get('/api/characters').headers['etag'] == saved.headers['etag']
