fn main() {
    divan::main();
}

#[divan::bench]
fn sum() -> u64 {
    bench_ws_member::sum(divan::black_box(1_000))
}
