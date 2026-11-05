// `<[T]>::as_chunks` is stable since Rust 1.88; the crate claims 1.85.
pub fn pairs(v: &[u8]) -> usize {
    let (chunks, _) = v.as_chunks::<2>();
    chunks.len()
}
