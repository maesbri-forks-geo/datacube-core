"""
Test utility functions from :module:`datacube.utils`


"""
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Tuple, Iterable

import numpy as np
import pytest
import toolz

from datacube.model import MetadataType
from datacube.model.utils import traverse_datasets, flatten_datasets, dedup_lineage, remap_lineage_doc
from datacube.testutils import mk_sample_product, make_graph_abcde, gen_dataset_test_dag, dataset_maker
from datacube.utils import (read_documents, InvalidDocException,
                            SimpleDocNav)
from datacube.utils.changes import check_doc_unchanged, get_doc_changes, MISSING, DocumentMismatchError
from datacube.utils.documents import parse_yaml, without_lineage_sources, _open_from_s3
from datacube.utils.serialise import jsonify_document
from datacube.utils.uris import as_url


doc_changes = [
    (1, 1, []),
    ({}, {}, []),
    ({'a': 1}, {'a': 1}, []),
    ({'a': {'b': 1}}, {'a': {'b': 1}}, []),
    ([1, 2, 3], [1, 2, 3], []),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 4, 5]], []),
    (1, 2, [((), 1, 2)]),
    ([1, 2, 3], [2, 1, 3], [((0,), 1, 2), ((1,), 2, 1)]),
    ([1, 2, [3, 4, 5]], [1, 2, [3, 6, 7]], [((2, 1), 4, 6), ((2, 2), 5, 7)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'a': 2}, [(('a',), 1, 2)]),
    ({'a': 1}, {'b': 1}, [(('a',), 1, MISSING), (('b',), MISSING, 1)]),
    ({'a': {'b': 1}}, {'a': {'b': 2}}, [(('a', 'b'), 1, 2)]),
    ({}, {'b': 1}, [(('b',), MISSING, 1)]),
    ({'a': {'c': 1}}, {'a': {'b': 1}}, [(('a', 'b'), MISSING, 1), (('a', 'c'), 1, MISSING)])
]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_get_doc_changes(v1, v2, expected):
    rval = get_doc_changes(v1, v2)
    assert rval == expected


def test_get_doc_changes_w_baseprefix():
    rval = get_doc_changes({}, None, base_prefix=('a',))
    assert rval == [(('a',), {}, None)]


@pytest.mark.parametrize("v1, v2, expected", doc_changes)
def test_check_doc_unchanged(v1, v2, expected):
    if expected != []:
        with pytest.raises(DocumentMismatchError):
            check_doc_unchanged(v1, v2, 'name')
    else:
        # No Error Raised
        check_doc_unchanged(v1, v2, 'name')


def test_more_check_doc_unchanged():
    # No exception raised
    check_doc_unchanged({'a': 1}, {'a': 1}, 'Letters')

    with pytest.raises(DocumentMismatchError, match='^Letters differs from stored.*a: 1!=2'):
        check_doc_unchanged({'a': 1}, {'a': 2}, 'Letters')

    with pytest.raises(DocumentMismatchError, match='^Letters differs from stored.*a.b: 1!=2'):
        check_doc_unchanged({'a': {'b': 1}}, {'a': {'b': 2}}, 'Letters')


def test_without_lineage_sources():
    def mk_sample(v):
        return dict(lineage={'source_datasets': v, 'a': 'a', 'b': 'b'},
                    aa='aa',
                    bb=dict(bb='bb'))

    spec = mk_sample_product('tt')

    x = {'a': 1}
    assert without_lineage_sources(x, spec) == x
    assert without_lineage_sources(x, spec, inplace=True) == x

    x = {'a': 1, 'lineage': {}}
    assert without_lineage_sources(x, spec) == x
    assert without_lineage_sources(x, spec, inplace=True) == x

    x = mk_sample(1)
    assert without_lineage_sources(x, spec) != x
    assert x['lineage']['source_datasets'] == 1

    x = mk_sample(2)
    assert without_lineage_sources(x, spec, inplace=True) == x
    assert x['lineage']['source_datasets'] == {}

    assert mk_sample(10) != mk_sample({})
    assert without_lineage_sources(mk_sample(10), spec) == mk_sample({})
    assert without_lineage_sources(mk_sample(10), spec, inplace=True) == mk_sample({})

    # check behaviour when `sources` is not defined for the type
    no_sources_type = MetadataType({
        'name': 'eo',
        'description': 'Sample',
        'dataset': dict(
            id=['id'],
            label=['ga_label'],
            creation_time=['creation_dt'],
            measurements=['image', 'bands'],
            format=['format', 'name'],
        )
    }, dataset_search_fields={})

    assert without_lineage_sources(mk_sample(10), no_sources_type) == mk_sample(10)
    assert without_lineage_sources(mk_sample(10), no_sources_type, inplace=True) == mk_sample(10)


def test_parse_yaml():
    assert parse_yaml('a: 10') == {'a': 10}


def test_read_docs_from_local_path(sample_document_files):
    _test_read_docs_impl(sample_document_files)


def test_read_docs_from_file_uris(sample_document_files):
    uris = [('file://' + doc, ndocs) for doc, ndocs in sample_document_files]
    _test_read_docs_impl(uris)


def test_read_docs_from_s3(sample_document_files, monkeypatch):
    """
    Use a mocked S3 bucket to test reading documents from S3
    """
    boto3 = pytest.importorskip('boto3')
    moto = pytest.importorskip('moto')

    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'fake')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'fake')

    with moto.mock_s3():
        s3 = boto3.resource('s3', region_name='us-east-1')
        bucket = s3.create_bucket(Bucket='mybucket')

        mocked_s3_objs = []
        for abs_fname, ndocs in sample_document_files:
            if abs_fname.endswith('gz') or abs_fname.endswith('nc'):
                continue

            fname = Path(abs_fname).name
            bucket.upload_file(abs_fname, fname)

            mocked_s3_objs.append(('s3://mybucket/' + fname, ndocs))

        _test_read_docs_impl(mocked_s3_objs)

    with pytest.raises(RuntimeError):
        with _open_from_s3("https://not-s3.ga/file.txt"):
            pass


def test_read_docs_from_http(sample_document_files, httpserver):
    http_docs = []
    for abs_fname, ndocs in sample_document_files:
        if abs_fname.endswith('gz') or abs_fname.endswith('nc'):
            continue
        path = "/" + Path(abs_fname).name

        httpserver.expect_request(path).respond_with_data(open(abs_fname).read())
        http_docs.append((httpserver.url_for(path), ndocs))

    _test_read_docs_impl(http_docs)


def _test_read_docs_impl(sample_documents: Iterable[Tuple[str, int]]):
    # Test case for returning URIs pointing to documents
    for doc_url, num_docs in sample_documents:
        all_docs = list(read_documents(doc_url, uri=True))
        assert len(all_docs) == num_docs

        for uri, doc in all_docs:
            assert isinstance(doc, dict)
            assert isinstance(uri, str)

        url = as_url(doc_url)
        if num_docs > 1:
            expect_uris = [as_url(url) + '#part={}'.format(i) for i in range(num_docs)]
        else:
            expect_uris = [as_url(url)]

        assert [f for f, _ in all_docs] == expect_uris


def test_dataset_maker():
    mk = dataset_maker(0)
    assert mk('aa') == mk('aa')

    a = SimpleDocNav(mk('A'))
    b = SimpleDocNav(mk('B'))

    assert a.id != b.id
    assert a.doc['creation_dt'] == b.doc['creation_dt']
    assert isinstance(a.id, str)
    assert a.sources == {}

    a1, a2 = [dataset_maker(i)('A', product_type='eo') for i in (0, 1)]
    assert a1['id'] != a2['id']
    assert a1['creation_dt'] != a2['creation_dt']
    assert a1['product_type'] == 'eo'

    c = SimpleDocNav(mk('C', sources=dict(a=a.doc, b=b.doc)))
    assert c.sources['a'].doc is a.doc
    assert c.sources['b'].doc is b.doc


def test_traverse_datasets():
    """
      A -> B
      |    |
      |    v
      +--> C -> D
      |
      +--> E
    """

    def node(name, **kwargs):
        return SimpleNamespace(id=name, sources=kwargs)

    A, *_ = make_graph_abcde(node)

    def visitor(node, name=None, depth=0, out=None):
        s = '{}:{}:{:d}'.format(node.id, name if name else '..', depth)
        out.append(s)

    with pytest.raises(ValueError):
        traverse_datasets(A, visitor, mode='not-a-real-mode')

    expect_preorder = '''
A:..:0
B:ab:1
C:bc:2
D:cd:3
C:ac:1
D:cd:2
E:ae:1
'''.lstrip().rstrip()

    expect_postorder = '''
D:cd:3
C:bc:2
B:ab:1
D:cd:2
C:ac:1
E:ae:1
A:..:0
'''.lstrip().rstrip()

    for mode, expect in zip(['pre-order', 'post-order'],
                            [expect_preorder, expect_postorder]):
        out = []
        traverse_datasets(A, visitor, mode=mode, out=out)
        assert '\n'.join(out) == expect

    fv = flatten_datasets(A)

    assert len(fv['A']) == 1
    assert len(fv['C']) == 2
    assert len(fv['E']) == 1
    assert set(fv.keys()) == set('ABCDE')


def test_simple_doc_nav():
    """
      A -> B
      |    |
      |    v
      +--> C -> D
      |
      +--> E
    """

    def node(name, **kwargs):
        return dict(id=name, lineage=dict(source_datasets=kwargs))

    A, _, C, _, _ = make_graph_abcde(node)
    rdr = SimpleDocNav(A)

    assert rdr.doc == A
    assert rdr.doc_without_lineage_sources == node('A')
    assert isinstance(rdr.sources['ae'], SimpleDocNav)
    assert rdr.sources['ab'].sources['bc'].doc == C
    assert rdr.doc_without_lineage_sources is rdr.doc_without_lineage_sources
    assert rdr.sources is rdr.sources
    assert isinstance(rdr.sources_path, tuple)

    def visitor(node, name=None, depth=0, out=None):
        s = '{}:{}:{:d}'.format(node.id, name if name else '..', depth)
        out.append(s)

    expect_preorder = '''
A:..:0
B:ab:1
C:bc:2
D:cd:3
C:ac:1
D:cd:2
E:ae:1
'''.lstrip().rstrip()

    expect_postorder = '''
D:cd:3
C:bc:2
B:ab:1
D:cd:2
C:ac:1
E:ae:1
A:..:0
'''.lstrip().rstrip()

    for mode, expect in zip(['pre-order', 'post-order'],
                            [expect_preorder, expect_postorder]):
        out = []
        traverse_datasets(rdr, visitor, mode=mode, out=out)
        assert '\n'.join(out) == expect

    fv = flatten_datasets(rdr)

    assert len(fv['A']) == 1
    assert len(fv['C']) == 2
    assert len(fv['E']) == 1
    assert set(fv.keys()) == set('ABCDE')

    fv, dg = flatten_datasets(rdr, with_depth_grouping=True)

    assert len(fv['A']) == 1
    assert len(fv['C']) == 2
    assert len(fv['E']) == 1
    assert set(fv.keys()) == set('ABCDE')
    assert isinstance(dg, list)
    assert len(dg) == 4
    assert [len(l) for l in dg] == [1, 3, 2, 1]

    def to_set(xx):
        return set(x.id for x in xx)

    assert [set(s) for s in ('A',
                             'BCE',
                             'CD',
                             'D')] == [to_set(xx) for xx in dg]

    with pytest.raises(ValueError):
        SimpleDocNav([])


def test_dedup():
    ds0 = SimpleDocNav(gen_dataset_test_dag(1, force_tree=True))

    # make sure ds0 has duplicate C nodes with equivalent data
    assert ds0.sources['ab'].sources['bc'].doc is not ds0.sources['ac'].doc
    assert ds0.sources['ab'].sources['bc'].doc == ds0.sources['ac'].doc

    ds = SimpleDocNav(dedup_lineage(ds0))
    assert ds.sources['ab'].sources['bc'].doc is ds.sources['ac'].doc
    assert ds.sources['ab'].sources['bc'].sources['cd'].doc is ds.sources['ac'].sources['cd'].doc

    # again but with raw doc
    ds = SimpleDocNav(dedup_lineage(ds0.doc))
    assert ds.sources['ab'].sources['bc'].doc is ds.sources['ac'].doc
    assert ds.sources['ab'].sources['bc'].sources['cd'].doc is ds.sources['ac'].sources['cd'].doc

    # Test that we detect inconsistent metadata for duplicate entries (test 1)
    # test: different values in the same spot
    ds0 = SimpleDocNav(gen_dataset_test_dag(3, force_tree=True))
    ds0.sources['ac'].doc['label'] = 'Modified'
    ds0 = SimpleDocNav(ds0.doc)
    assert ds0.sources['ab'].sources['bc'].doc != ds0.sources['ac'].doc

    with pytest.raises(InvalidDocException, match=r'Inconsistent metadata .*'):
        dedup_lineage(ds0)

    # Test that we detect inconsistent metadata for duplicate entries (test 2)
    # test: different sources structure
    ds0 = SimpleDocNav(gen_dataset_test_dag(3, force_tree=True))
    ds0.sources['ac'].doc['lineage']['source_datasets']['extra'] = ds0.sources['ae'].doc.copy()
    assert ds0.sources['ab'].sources['bc'].doc != ds0.sources['ac'].doc

    ds0 = SimpleDocNav(ds0.doc)

    with pytest.raises(InvalidDocException, match=r'Inconsistent lineage .*'):
        dedup_lineage(ds0)

    # Test that we detect inconsistent lineage subtrees for duplicate entries

    # Subtest 1: different set of keys
    ds0 = SimpleDocNav(gen_dataset_test_dag(7, force_tree=True))
    srcs = toolz.get_in(ds0.sources_path, ds0.sources['ac'].doc)

    assert 'cd' in srcs
    srcs['cd'] = {}
    ds0 = SimpleDocNav(ds0.doc)

    with pytest.raises(InvalidDocException, match=r'Inconsistent lineage .*'):
        dedup_lineage(ds0)

    # Subtest 2: different values for "child" nodes
    ds0 = SimpleDocNav(gen_dataset_test_dag(7, force_tree=True))
    srcs = toolz.get_in(ds0.sources_path, ds0.sources['ac'].doc)

    assert 'cd' in srcs
    srcs['cd']['id'] = '7fe57724-ed44-4beb-a3ab-c275339049be'
    ds0 = SimpleDocNav(ds0.doc)

    with pytest.raises(InvalidDocException, match=r'Inconsistent lineage .*'):
        dedup_lineage(ds0)

    # Subtest 3: different name for child
    ds0 = SimpleDocNav(gen_dataset_test_dag(7, force_tree=True))
    srcs = toolz.get_in(ds0.sources_path, ds0.sources['ac'].doc)

    assert 'cd' in srcs
    srcs['CD'] = srcs['cd']
    del srcs['cd']
    ds0 = SimpleDocNav(ds0.doc)

    with pytest.raises(InvalidDocException, match=r'Inconsistent lineage .*'):
        dedup_lineage(ds0)


def test_remap_lineage_doc():
    def mk_node(ds, sources):
        return dict(id=ds.id, **sources)

    ds = SimpleDocNav(gen_dataset_test_dag(3, force_tree=True))
    xx = remap_lineage_doc(ds, mk_node)
    assert xx['id'] == ds.id
    assert xx['ac']['id'] == ds.sources['ac'].id

    xx = remap_lineage_doc(ds.doc, mk_node)
    assert xx['id'] == ds.id
    assert xx['ac']['id'] == ds.sources['ac'].id


def test_merge():
    from datacube.model.utils import merge
    assert merge(dict(a=1), dict(b=2)) == dict(a=1, b=2)
    assert merge(dict(a=1, b=2), dict(b=2)) == dict(a=1, b=2)

    with pytest.raises(Exception):
        merge(dict(a=1, b=2), dict(b=3))


@pytest.mark.xfail(True, reason="Merging dictionaries with content of NaN doesn't work currently")
def test_merge_with_nan():
    from datacube.model.utils import merge

    _nan = float("nan")
    assert _nan != _nan
    xx = merge(dict(a=_nan), dict(a=_nan))  # <- fails here because of simple equality check
    assert xx['a'] != xx['a']


@pytest.fixture
def sample_document_files(data_folder):
    files = [('multi_doc.yml', 3),
             ('multi_doc.yml.gz', 3),
             ('multi_doc.nc', 3),
             ('single_doc.yaml', 1),
             ('sample.json', 1)]

    files = [(str(os.path.join(data_folder, f)), num_docs)
             for f, num_docs in files]

    return files


def test_jsonify():
    from datetime import datetime
    from uuid import UUID
    from decimal import Decimal

    assert sorted(jsonify_document({'a': (1.0, 2.0, 3.0),
                                    'b': float("inf"),
                                    'c': datetime(2016, 3, 11),
                                    'd': np.dtype('int16'),
                                    }).items()) == [
                                    ('a', (1.0, 2.0, 3.0)),
                                    ('b', 'Infinity'),
                                    ('c', '2016-03-11T00:00:00'),
                                    ('d', 'int16'),
                                    ]

    # Converts keys to strings:
    assert sorted(jsonify_document({1: 'a', '2': Decimal('2')}).items()) == [
        ('1', 'a'), ('2', '2')]

    assert jsonify_document({'k': UUID("1f231570-e777-11e6-820f-185e0f80a5c0")}) == {
        'k': '1f231570-e777-11e6-820f-185e0f80a5c0'}
