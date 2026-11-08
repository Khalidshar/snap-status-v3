from studio_core.production.prepare import cut_matches


def test_cut_must_match_the_same_ayah_elsewhere():
    kursi = [54.6, 41.9, 52.0]  # 2:255 from three per-ayah reciters
    assert cut_matches(57.5, kursi)
    assert not cut_matches(26.3, kursi)  # 2:256 would be refused
    assert not cut_matches(120.0, kursi)


def test_no_reference_means_no_belief():
    assert not cut_matches(55.0, [])
