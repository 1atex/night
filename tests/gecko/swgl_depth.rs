use gleam::{gl, gl::Gl};
use std::ffi::c_void;
use webrender::api::{units::*, ColorF, ExternalImageId, ImageRendering};
use webrender::sw_compositor::SwCompositor;
use webrender::{
    ClipRadius, Compositor, CompositorCapabilities, CompositorSurfaceTransform, MappableCompositor,
    MappedTileInfo, NativeSurfaceId, NativeSurfaceInfo, NativeTileId, SWGLCompositeSurfaceInfo,
    WindowVisibility,
};

struct Surface(Vec<u32>);
impl Compositor for Surface {
    fn create_surface(&mut self, _: NativeSurfaceId, _: DeviceIntSize, _: bool) {}
    fn create_external_surface(&mut self, _: NativeSurfaceId, _: bool) {
        unreachable!()
    }
    fn create_backdrop_surface(&mut self, _: NativeSurfaceId, _: ColorF) {
        unreachable!()
    }
    fn destroy_surface(&mut self, _: NativeSurfaceId) {}
    fn create_tile(&mut self, _: NativeTileId) {}
    fn destroy_tile(&mut self, _: NativeTileId) {}
    fn attach_external_image(&mut self, _: NativeSurfaceId, _: ExternalImageId) {
        unreachable!()
    }
    fn bind(&mut self, _: NativeTileId, _: DeviceIntRect, _: DeviceIntRect) -> NativeSurfaceInfo {
        unreachable!()
    }
    fn unbind(&mut self) {
        unreachable!()
    }
    fn begin_frame(&mut self) {}
    fn add_surface(
        &mut self,
        _: NativeSurfaceId,
        _: CompositorSurfaceTransform,
        _: DeviceIntRect,
        _: ImageRendering,
        _: DeviceIntRect,
        _: ClipRadius,
    ) {
        unreachable!()
    }
    fn end_frame(&mut self) {}
    fn deinit(&mut self) {}
    fn get_capabilities(&self) -> CompositorCapabilities {
        Default::default()
    }
    fn get_window_visibility(&self) -> WindowVisibility {
        Default::default()
    }
}
impl MappableCompositor for Surface {
    fn map_tile(
        &mut self,
        _: NativeTileId,
        _: DeviceIntRect,
        valid: DeviceIntRect,
    ) -> Option<MappedTileInfo> {
        self.0.resize((valid.width() * valid.height()) as usize, 0);
        self.0.fill(0);
        Some(MappedTileInfo {
            data: self.0.as_mut_ptr().cast(),
            stride: valid.width() * 4,
        })
    }
    fn unmap_tile(&mut self) {
        assert!(self.0.iter().all(|p| *p == 0xffffffff));
    }
    fn lock_composite_surface(
        &mut self,
        _: *mut c_void,
        _: ExternalImageId,
        _: *mut SWGLCompositeSurfaceInfo,
    ) -> bool {
        unreachable!()
    }
    fn unlock_composite_surface(&mut self, _: *mut c_void, _: ExternalImageId) {
        unreachable!()
    }
}
extern "C" {
    fn malloc_usable_size(p: *mut c_void) -> usize;
}
unsafe extern "C" fn allocated(p: *const c_void) -> usize {
    if p.is_null() {
        0
    } else {
        malloc_usable_size(p.cast_mut())
    }
}
fn main() {
    let gl = swgl::Context::create();
    gl.make_current();
    let mut compositor = SwCompositor::new(gl, Box::new(Surface(vec![])), true);
    let mut sizes = vec![(1024, 512)];
    if std::env::args().any(|arg| arg == "--scrollbars") {
        sizes.extend([(32, 1024), (1024, 32)]);
    }
    for (i, &(w, h)) in sizes.iter().enumerate() {
        let sid = NativeSurfaceId(i as u64 + 1);
        compositor.create_surface(sid, DeviceIntSize::new(w, h), true);
        compositor.create_tile(NativeTileId {
            surface_id: sid,
            x: 0,
            y: 0,
        });
    }
    let mut peak = 0;
    for pass in 0..8 {
        for (i, &(w, h)) in sizes.iter().enumerate() {
            let id = NativeTileId {
                surface_id: NativeSurfaceId(i as u64 + 1),
                x: 0,
                y: 0,
            };
            let inset = pass % 2;
            let r = DeviceIntRect::from_size(DeviceIntSize::new(w - inset, h - inset));
            compositor.invalidate_tile(id, r);
            let info = compositor.bind(id, r, r);
            gl.bind_framebuffer(gl::DRAW_FRAMEBUFFER, info.handle.0 as u32);
            gl.clear_color(1., 1., 1., 1.);
            gl.clear_depth(0.5);
            gl.clear(gl::COLOR_BUFFER_BIT | gl::DEPTH_BUFFER_BIT);
            compositor.unbind();
            peak = peak.max(gl.report_memory(allocated));
        }
    }
    for i in 0..sizes.len() {
        compositor.destroy_surface(NativeSurfaceId(i as u64 + 1));
    }
    assert_eq!(gl.report_memory(allocated), 0);
    compositor.deinit();
    gl.destroy();
    println!("{peak}");
}
