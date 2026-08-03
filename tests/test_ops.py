from ifc_occam.core.ops import (
    Operation,
    ops_to_json,
    ops_from_json,
    resolve_effective,
    validate_operations,
)


def test_operation_to_dict_and_from_dict_roundtrip():
    op = Operation(op="simplify", targets=["G1", "G2"], scope="shared",
                    params={"method": "bbox", "ratio": 0.3})
    d = op.to_dict()
    assert d == {
        "op": "simplify",
        "targets": ["G1", "G2"],
        "scope": "shared",
        "params": {"method": "bbox", "ratio": 0.3},
    }
    restored = Operation.from_dict(d)
    assert restored == op


def test_operation_defaults():
    op = Operation(op="keep", targets=["G1"])
    assert op.scope == "element"
    assert op.params == {}


def test_ops_to_json_and_from_json_roundtrip_preserves_order_and_params():
    ops = [
        Operation(op="delete", targets=["G1"]),
        Operation(op="simplify", targets=["G2"], scope="shared",
                   params={"method": "decimate", "ratio": 0.5}),
        Operation(op="keep", targets=["G3"]),
    ]
    payload = ops_to_json(ops)
    restored = ops_from_json(payload)
    assert restored == ops


def test_resolve_effective_last_wins_delete_then_keep_then_simplify():
    """delete -> keep -> simplify の順で同じgidに来たら simplify が有効。"""
    ops = [
        Operation(op="delete", targets=["G1"]),
        Operation(op="keep", targets=["G1"]),
        Operation(op="simplify", targets=["G1"], params={"method": "bbox"}),
    ]
    effective = resolve_effective(ops)
    assert effective["G1"].op == "simplify"


def test_resolve_effective_keep_alone_is_kept_in_result():
    """keep 単独は「対象外に確定」として resolve に含まれる(op="keep")。"""
    ops = [Operation(op="keep", targets=["G1"])]
    effective = resolve_effective(ops)
    assert "G1" in effective
    assert effective["G1"].op == "keep"


def test_resolve_effective_keep_after_delete_cancels_delete():
    ops = [
        Operation(op="delete", targets=["G1"]),
        Operation(op="keep", targets=["G1"]),
    ]
    effective = resolve_effective(ops)
    assert effective["G1"].op == "keep"


def test_resolve_effective_multiple_gids_independent():
    ops = [
        Operation(op="delete", targets=["G1", "G2"]),
        Operation(op="keep", targets=["G2"]),
    ]
    effective = resolve_effective(ops)
    assert effective["G1"].op == "delete"
    assert effective["G2"].op == "keep"


def test_resolve_effective_empty_operations_returns_empty_dict():
    assert resolve_effective([]) == {}


def test_validate_operations_valid_list_returns_empty():
    known = {"G1", "G2"}
    ops = [
        Operation(op="delete", targets=["G1"]),
        Operation(op="simplify", targets=["G2"],
                   params={"method": "bbox"}),
        Operation(op="keep", targets=["G1"]),
    ]
    assert validate_operations(ops, known) == []


def test_validate_operations_warns_on_unknown_gid():
    known = {"G1"}
    ops = [Operation(op="delete", targets=["G1", "UNKNOWN"])]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1
    assert "UNKNOWN" in warnings[0]


def test_validate_operations_warns_on_invalid_op():
    known = {"G1"}
    ops = [Operation(op="bogus", targets=["G1"])]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1
    assert "bogus" in warnings[0]


def test_validate_operations_warns_on_invalid_scope():
    known = {"G1"}
    ops = [Operation(op="delete", targets=["G1"], scope="global")]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1
    assert "global" in warnings[0]


def test_validate_operations_warns_on_invalid_simplify_method():
    known = {"G1"}
    ops = [Operation(op="simplify", targets=["G1"], params={"method": "nope"})]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1
    assert "nope" in warnings[0]


def test_validate_operations_warns_on_decimate_without_ratio():
    known = {"G1"}
    ops = [Operation(op="simplify", targets=["G1"], params={"method": "decimate"})]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1


def test_validate_operations_warns_on_decimate_with_out_of_range_ratio():
    known = {"G1"}
    ops = [Operation(op="simplify", targets=["G1"],
                      params={"method": "decimate", "ratio": 1.5})]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1


def test_validate_operations_decimate_with_valid_ratio_ok():
    known = {"G1"}
    ops = [Operation(op="simplify", targets=["G1"],
                      params={"method": "decimate", "ratio": 0.5})]
    assert validate_operations(ops, known) == []


def test_validate_operations_bbox_and_convex_hull_do_not_require_ratio():
    known = {"G1", "G2"}
    ops = [
        Operation(op="simplify", targets=["G1"], params={"method": "bbox"}),
        Operation(op="simplify", targets=["G2"], params={"method": "convex_hull"}),
    ]
    assert validate_operations(ops, known) == []


def test_validate_operations_obbは有効なmethod():
    op = Operation(op="simplify", targets=["gid1"], params={"method": "obb"})
    assert validate_operations([op], {"gid1"}) == []


def test_validate_operations_accumulates_multiple_warnings():
    known = {"G1"}
    ops = [
        Operation(op="delete", targets=["UNKNOWN1"]),
        Operation(op="bogus", targets=["G1"]),
    ]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 2


def test_validate_operations_does_not_raise_on_malformed_input():
    known = {"G1"}
    ops = [Operation(op="simplify", targets=["G1"], params={"method": "decimate", "ratio": "not-a-number"})]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 1


def test_validate_operations_reports_all_independent_warnings_for_one_operation():
    """invalid op + invalid scope + unknown gid は独立したチェックなので全て報告される。"""
    known = {"G1"}
    ops = [Operation(op="explode", targets=["UNKNOWN_GID"], scope="banana")]
    warnings = validate_operations(ops, known)
    assert len(warnings) == 3
    joined = " ".join(warnings)
    assert "explode" in joined
    assert "banana" in joined
    assert "UNKNOWN_GID" in joined


def test_from_dict_defaults_when_scope_and_params_missing():
    d = {"op": "delete", "targets": ["G1"]}
    op = Operation.from_dict(d)
    assert op.scope == "element"
    assert op.params == {}


def test_ops_to_json_and_from_json_roundtrip_non_ascii_params():
    ops = [Operation(op="keep", targets=["G1"], params={"note": "配管"})]
    payload = ops_to_json(ops)
    restored = ops_from_json(payload)
    assert restored == ops
