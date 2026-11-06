fn main() {
    divan::main();
}

#[divan::bench]
fn sum() -> u64 {
    bench_fixture::sum(divan::black_box(1_000))
}
