from app.repository import upsert_embedding, vector_literal


class FakePool:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, query, *args) -> None:
        self.calls.append((query, args))


def test_vector_literal() -> None:
    assert vector_literal([0.25, -1.0, 0.0]) == "[0.25,-1,0]"


async def test_upsert_binds_molecule_and_model() -> None:
    pool = FakePool()
    await upsert_embedding(
        pool,
        experiment={"id": "e1", "algorithm": "vqe", "parameters": {"molecule": "lih"}},
        content="Molecule: lih",
        embedding=[0.1, 0.2],
        model_name="test-model",
    )
    query, args = pool.calls[0]
    assert "ON CONFLICT (experiment_id) DO UPDATE" in query
    assert args == ("e1", "vqe", "lih", "Molecule: lih", "[0.1,0.2]", "test-model")
