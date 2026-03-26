from lookout.enrich.tagger import infer_product_type, infer_tags


def test_infer_ski():
    assert infer_product_type("Prodigy 3 Ski") == "Ski"


def test_infer_ski_boot():
    assert infer_product_type("Speedmachine 3 Ski Boot") == "Ski Boot"


def test_infer_jacket():
    assert infer_product_type("Crista 3L Jacket") == "Jacket"


def test_infer_insulated_jacket():
    assert infer_product_type("Nano Puff Jacket", "insulated jacket with PrimaLoft") == "Insulated Jacket"


def test_infer_goggle():
    assert infer_product_type("I/O MAG Snow Goggles") == "Goggle"


def test_infer_snowboard():
    assert infer_product_type("Mind Expander Snowboard") == "Snowboard"


def test_infer_none():
    assert infer_product_type("Widget Thing") is None


def test_ski_not_ski_boot():
    # "ski boot" should match Ski Boot, not Ski
    assert infer_product_type("Speedmachine Ski Boot") == "Ski Boot"


def test_infer_gender_mens():
    tags = infer_tags("Men's Crista 3L Jacket")
    assert "mens" in tags


def test_infer_gender_womens():
    tags = infer_tags("Women's Nano Puff Jacket")
    assert "womens" in tags


def test_infer_activity_skiing():
    tags = infer_tags("Prodigy 3 Ski")
    assert "skiing" in tags


def test_infer_activity_hiking():
    tags = infer_tags("Lone Peak 8 Trail Runner")
    assert "hiking" in tags or "running" in tags


def test_no_duplicate_tags():
    tags = infer_tags("Men's Jacket", existing_tags="mens, jacket")
    assert "mens" not in tags  # already exists


def test_neckwarmer():
    assert infer_product_type("Vuarnet Neckwarmer") == "Neck Gaiter"
