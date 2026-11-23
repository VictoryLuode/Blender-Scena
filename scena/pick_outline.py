"""Draw the boundary of a picked object's projected geometry in screen pixels."""

import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader


COLOR = (0.30, 0.95, 0.40, 1.0)
MESH_TYPES = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META', 'CURVES'}


def _edge_shader():
    info = gpu.types.GPUShaderCreateInfo()
    varying = gpu.types.GPUStageInterfaceInfo("scena_pick_uv")
    varying.smooth('VEC2', "uv")
    info.vertex_in(0, 'VEC2', "pos")
    info.vertex_out(varying)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.sampler(0, 'FLOAT_2D', "maskTex")
    info.push_constant('VEC4', "tint")
    info.push_constant('FLOAT', "lineWidth")
    info.vertex_source("""
        void main() {
            uv = pos * 0.5 + 0.5;
            gl_Position = vec4(pos, 0.0, 1.0);
        }
    """)
    info.fragment_source("""
        void main() {
            ivec2 size = textureSize(maskTex, 0);
            ivec2 center = clamp(ivec2(uv * vec2(size)), ivec2(0), size - 1);
            if (texelFetch(maskTex, center, 0).r > 0.5) discard;
            float coverage = 0.0;
            int radius = int(ceil(lineWidth));
            for (int y = -radius; y <= radius; ++y) {
                for (int x = -radius; x <= radius; ++x) {
                    ivec2 offset = ivec2(x, y);
                    float weight = clamp(lineWidth + 1.0 - length(vec2(offset)), 0.0, 1.0);
                    ivec2 neighbor = clamp(center + offset, ivec2(0), size - 1);
                    coverage = max(coverage, weight * texelFetch(maskTex, neighbor, 0).r);
                }
            }
            fragColor = vec4(tint.rgb, tint.a * coverage);
        }
    """)
    return gpu.shader.create_from_info(info)


def _geometry(obj, shader):
    if obj.type not in MESH_TYPES:
        return None
    mesh = obj.data if obj.type == 'MESH' else obj.to_mesh()
    if mesh is None:
        return None
    try:
        if not mesh.vertices:
            return None
        coords = np.empty((len(mesh.vertices), 3), dtype=np.float32)
        mesh.vertices.foreach_get("co", coords.ravel())
        mesh.calc_loop_triangles()
        if mesh.loop_triangles:
            primitive, elements, width = 'TRIS', mesh.loop_triangles, 3
        elif mesh.edges:
            primitive, elements, width = 'LINES', mesh.edges, 2
        else:
            return batch_for_shader(shader, 'POINTS', {"pos": coords})
        indices = np.empty((len(elements), width), dtype=np.int32)
        elements.foreach_get("vertices", indices.ravel())
        return batch_for_shader(shader, primitive, {"pos": coords}, indices=indices)
    finally:
        if obj.type != 'MESH':
            obj.to_mesh_clear()


class PickOutline:
    def __init__(self):
        self.solid = gpu.shader.from_builtin('UNIFORM_COLOR')
        self.edge = _edge_shader()
        self.quad = batch_for_shader(self.edge, 'TRI_STRIP', {"pos": [(-1, -1), (1, -1), (-1, 1), (1, 1)]})
        self.obj = None
        self.size = None

    def draw(self, obj, depsgraph, matrix, projection, size, width):
        if obj != self.obj:
            self.batch = _geometry(obj.evaluated_get(depsgraph), self.solid)
            self.obj = obj
        if self.batch is None:
            return False
        if size != self.size:
            self.mask = gpu.types.GPUTexture(size, format='R8')
            self.frame = gpu.types.GPUFrameBuffer(color_slots=(self.mask,))
            self.size = size
        viewport = gpu.state.viewport_get()
        scissor = gpu.state.scissor_get()
        depth, blend = gpu.state.depth_test_get(), gpu.state.blend_get()
        try:
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
            with self.frame.bind():
                gpu.state.viewport_set(0, 0, *size)
                gpu.state.scissor_set(0, 0, *size)
                self.frame.clear(color=(0, 0, 0, 0))
                with gpu.matrix.push_pop(), gpu.matrix.push_pop_projection():
                    gpu.matrix.load_projection_matrix(projection)
                    gpu.matrix.load_matrix(matrix)
                    self.solid.bind()
                    self.solid.uniform_float("color", (1, 1, 1, 1))
                    self.batch.draw(self.solid)
            gpu.state.viewport_set(*viewport)
            gpu.state.scissor_set(*scissor)
            gpu.state.blend_set('ALPHA')
            self.edge.bind()
            self.edge.uniform_sampler("maskTex", self.mask)
            self.edge.uniform_float("tint", COLOR)
            self.edge.uniform_float("lineWidth", width)
            self.quad.draw(self.edge)
        finally:
            gpu.state.viewport_set(*viewport)
            gpu.state.scissor_set(*scissor)
            gpu.state.blend_set(blend)
            gpu.state.depth_test_set(depth)
        return True
