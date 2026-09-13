from yoursql.common import Column, DataType, Schema, Value


def test_schema_is_case_insensitive_and_coerces_rows() -> None:
    schema = Schema.from_iterable(
        [
            Column("id", DataType.INT, nullable=False),
            Column("name", DataType.VARCHAR),
            Column("active", DataType.BOOLEAN, default=Value(DataType.BOOLEAN, True)),
        ]
    )

    assert schema.index("ID") == 0
    assert schema.validate_row((1.0, "Alice")) == (1, "Alice", True)


def test_value_inference() -> None:
    assert Value.infer(True).data_type is DataType.BOOLEAN
    assert Value.infer(3).data_type is DataType.INT
    assert Value.infer(3.5).data_type is DataType.FLOAT
    assert Value.infer(None).data_type is DataType.NULL
