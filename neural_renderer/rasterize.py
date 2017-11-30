import chainer
import chainer.functions as cf


class Rasterize(chainer.Function):
    def __init__(self, image_size, near, far, eps, background_color):
        super(Rasterize, self).__init__()
        self.image_size = image_size
        self.near = near
        self.far = far
        self.eps = eps
        self.background_color = background_color

        self.face_index_map = None
        self.weight_map = None
        self.face_map = None
        self.z_map = None
        self.sampling_weight_map = None
        self.sampling_index_map = None
        self.images = None

    def check_type_forward(self, in_types):
        chainer.utils.type_check.expect(in_types.size() == 2)
        faces_type, textures_type = in_types

        chainer.utils.type_check.expect(
            faces_type.dtype.kind == 'f',
            faces_type.ndim == 4,
            faces_type.shape[2] == 3,
            faces_type.shape[3] == 3,
        )

        chainer.utils.type_check.expect(
            textures_type.dtype.kind == 'f',
            textures_type.ndim == 6,
            textures_type.shape[2] == textures_type.shape[3],
            textures_type.shape[3] == textures_type.shape[4],
            textures_type.shape[5] == 3,
        )

        chainer.utils.type_check.expect(
            faces_type.shape[0] == textures_type.shape[0],
            faces_type.shape[1] == textures_type.shape[1],
        )

    def forward_gpu(self, inputs):
        xp = chainer.cuda.get_array_module(inputs[0])
        faces = xp.ascontiguousarray(inputs[0])
        textures = xp.ascontiguousarray(inputs[1])
        bs, nf = faces.shape[:2]
        is_ = self.image_size
        ts = textures.shape[2]

        # initialize buffers
        self.face_index_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_), dtype='int32')) - 1
        self.weight_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_, 3), dtype='float32'))
        self.face_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_, 3, 3), dtype='float32'))
        self.z_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_), dtype='float32'))
        self.sampling_weight_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_, 8), 'float32'))
        self.sampling_index_map = xp.ascontiguousarray(xp.zeros((bs, is_, is_, 8), 'int32'))
        self.images = xp.ascontiguousarray(xp.zeros((bs, is_, is_, 3), dtype='float32'))

        # vertices -> face_index_map, z_map
        # face_index_map = -1 if background
        chainer.cuda.elementwise(
            'raw float32 faces, int32 num_faces, int32 image_size, float32 near, float32 far',
            'int32 face_index_map, raw float32 weight_map, raw float32 face_map, float32 z_map',
            '''
                /* current position & index */
                const int nf = num_faces;                   // short name
                const int is = image_size;                  // short name
                const int is2 = is * is;                    // number of pixels
                const int pi = i;                           // pixel index on all batches
                const int bn = pi / (is2);                  // batch number
                const int pyi = (pi % (is2)) / is;          // index of current y-position [0, is - 1]
                const int pxi = (pi % (is2)) % is;          // index of current x-position [0, is - 1]
                const float py = (1 - 1. / is) * ((2. / (is - 1)) * pyi - 1);   // coordinate of y-position [-1, 1]
                const float px = (1 - 1. / is) * ((2. / (is - 1)) * pxi - 1);   // coordinate of x-position [-1, 1]

                /* for each face */
                float* face;            // current face
                float z_min = far;      // z of nearest face
                int z_min_fn = -1;      // face number of nearest face
                float z_min_weight[3];  //
                float z_min_face[9];    //
                for (int fn = 0; fn < nf; fn++) {
                    /* go to next face */
                    if (fn == 0) {
                        face = &faces[(bn * nf) * 3 * 3];
                    } else {
                        face += 3 * 3;
                    }

                    /* get vertex of current face */
                    const float x[3] = {face[0], face[3], face[6]};
                    const float y[3] = {face[1], face[4], face[7]};
                    const float z[3] = {face[2], face[5], face[8]};

                    /* check too close & too far */
                    if (z[0] <= 0 || z[1] <= 0 || z[2] <= 0) continue;
                    if (z_min < z[0] && z_min < z[1] && z_min < z[2]) continue;

                    /* check [py, px] is inside the face */
                    if (((py - y[0]) * (x[1] - x[0]) < (px - x[0]) * (y[1] - y[0])) ||
                        ((py - y[1]) * (x[2] - x[1]) < (px - x[1]) * (y[2] - y[1])) ||
                        ((py - y[2]) * (x[0] - x[2]) < (px - x[2]) * (y[0] - y[2]))) continue;

                    /* compute f_inv */
                    float f_inv[9] = {
                        y[1] - y[2], x[2] - x[1], x[1] * y[2] - x[2] * y[1],
                        y[2] - y[0], x[0] - x[2], x[2] * y[0] - x[0] * y[2],
                        y[0] - y[1], x[1] - x[0], x[0] * y[1] - x[1] * y[0]};
                    float f_inv_denominator = x[2] * (y[0] - y[1]) + x[0] * (y[1] - y[2]) + x[1] * (y[2] - y[0]);
                    for (int k = 0; k < 9; k++) f_inv[k] /= f_inv_denominator;

                    /* compute w = f_inv * p */
                    float w[3];
                    for (int k = 0; k < 3; k++) w[k] = f_inv[3 * k + 0] * px + f_inv[3 * k + 1] * py + f_inv[3 * k + 2];

                    /* sum(w) -> 1, 0 < w < 1 */
                    float w_sum = 0;
                    for (int k = 0; k < 3; k++) {
                        if (w[k] < 0) w[k] = 0;
                        if (1 < w[k]) w[k] = 1;
                        w_sum += w[k];
                    }
                    for (int k = 0; k < 3; k++) w[k] /= w_sum;

                    /* compute 1 / zp = sum(w / z) & check z-buffer */
                    const float zp = 1. / (w[0] / z[0] + w[1] / z[1] + w[2] / z[2]);
                    if (zp <= near || far <= zp) continue;

                    /* check nearest */
                    if (zp < z_min) {
                        z_min = zp;
                        z_min_fn = fn;
                        memcpy(z_min_weight, w, 3 * sizeof(float));
                        memcpy(z_min_face, face, 9 * sizeof(float));
                    }
                }
                /* set to buffer */
                if (0 <= z_min_fn) {
                    face_index_map = z_min_fn;
                    z_map = z_min;
                    memcpy(&weight_map[pi * 3], z_min_weight, 3 * sizeof(float));
                    memcpy(&face_map[pi * 9], z_min_face, 9 * sizeof(float));
                }
            ''',
            'function',
        )(
            faces, nf, is_, self.near, self.far, self.face_index_map.ravel(), self.weight_map, self.face_map,
            self.z_map.ravel(),
        )

        # texture sampling
        background_colors = xp.array(self.background_color, 'float32')
        chainer.cuda.elementwise(
            'int32 pi, raw float32 textures, raw float32 face_map, int32 face_index, raw float32 weight_map, ' +
            'float32 z, int32 image_size, int32 num_faces, int32 texture_size, raw float32 background_color, ' +
            'float32 eps',
            'raw float32 images, raw float32 sampling_weight_map, raw int32 sampling_index_map',
            '''
                int is = image_size;
                int nf = num_faces;
                int ts = texture_size;
                int bn = pi / (is * is);

                float* pixel = &images[pi * 3];
                if (0 <= face_index) {
                    float* face = &face_map[pi * 9];
                    float* weight = &weight_map[pi * 3];
                    float* texture = &textures[(bn * nf + face_index) * ts * ts * ts * 3];
                    float new_pixel[3] = {0, 0, 0};

                    /* get texture index (float) */
                    float texture_index_float[3];
                    for (int k = 0; k < 3; k++) {
                        texture_index_float[k] = weight[k] * (ts - 1 - eps) * (z / (face[2 + 3 * k]));
                    }
                    for (int pn = 0; pn < 8; pn++) {
                        /* blend */
                        float w = 1;                        // weight
                        int texture_index_int[3];            // index in source (int)
                        for (int k = 0; k < 3; k++) {
                            if ((pn >> k) % 2 == 0) {
                                w *= 1 - (texture_index_float[k] - (int)texture_index_float[k]);
                                texture_index_int[k] = (int)texture_index_float[k];
                            } else {
                                w *= texture_index_float[k] - (int)texture_index_float[k];
                                texture_index_int[k] = (int)texture_index_float[k] + 1;
                            }
                        }

                        int isc = texture_index_int[0] * ts * ts + texture_index_int[1] * ts + texture_index_int[2];
                        for (int k = 0; k < 3; k++) new_pixel[k] += w * texture[isc * 3 + k];
                        sampling_weight_map[pi * 8 + pn] = w;
                        sampling_index_map[pi * 8 + pn] = isc;
                    }
                    memcpy(pixel, new_pixel, 3 * sizeof(float));
                } else {
                    for (int k = 0; k < 3; k++) pixel[k] = background_color[k];
                }
            ''',
            'function',
        )(
            xp.arange(bs * is_ * is_).astype('int32'), textures, self.face_map, self.face_index_map.ravel(),
            self.weight_map, self.z_map.ravel(), is_, nf, ts, background_colors, self.eps,
            self.images, self.sampling_weight_map, self.sampling_index_map,
        )
        return self.images,

    def backward_gpu(self, inputs, grad_outputs):
        xp = chainer.cuda.get_array_module(inputs[0])
        faces = xp.ascontiguousarray(inputs[0])
        textures = xp.ascontiguousarray(inputs[1])
        grad_images = xp.ascontiguousarray(grad_outputs[0])
        grad_faces = xp.ascontiguousarray(xp.zeros_like(faces, dtype='float32'))
        grad_textures = xp.ascontiguousarray(xp.zeros_like(textures, dtype='float32'))
        bs, nf = faces.shape[:2]
        is_ = self.image_size
        ts = textures.shape[2]

        # backward texture sampling
        chainer.cuda.elementwise(
            'int32 pi, int32 face_index, raw T sampling_weight_map, raw int32 sampling_index_map, ' +
            'raw T grad_images, raw int32 image_size, raw int32 num_faces, raw int32 texture_size',
            'raw T grad_textures',
            '''
                int is = image_size;
                int nf = num_faces;
                int ts = texture_size;
                int bn = pi / (is * is);    // batch number [0 -> bs]

                if (0 <= face_index) {
                    float* grad_texture = &grad_textures[(bn * nf + face_index) * ts * ts * ts * 3];
                    for (int pn = 0; pn < 8; pn++) {
                        float w = sampling_weight_map[pi * 8 + pn];
                        int isc = sampling_index_map[pi * 8 + pn];
                        for (int k = 0; k < 3; k++) atomicAdd(&grad_texture[isc * 3 + k], w * grad_images[pi * 3 + k]);
                    }
                }
            ''',
            'function',
        )(
            xp.arange(bs * is_ * is_).astype('int32'), self.face_index_map.ravel(), self.sampling_weight_map,
            self.sampling_index_map, grad_images, is_, nf, ts, grad_textures,
        )

        # pseudo gradient
        chainer.cuda.elementwise(
            'int32 j, raw float32 faces, raw int32 face_index_map, raw float32 images, ' +
            'raw float32 grad_images, int32 image_size, int32 num_faces, float32 eps ',
            'raw float32 grad_faces',
            '''
                /* exit if no gradient from upper layers */
                const float* grad_pixel = &grad_images[3 * j];
                const float grad_pixel_sum = grad_pixel[0] + grad_pixel[1] + grad_pixel[2];
                if (grad_pixel_sum == 0) return;

                /* compute current position & index */
                const int nf = num_faces;
                const int is = image_size;
                const int is2 = is * is;                    // number of pixels
                const int pi = j;                           // pixel index on all batches
                const int bn = pi / (is2);                  // batch number
                const int pyi = (pi % (is2)) / is;          // index of current y-position [0, is]
                const int pxi = (pi % (is2)) % is;          // index of current x-position [0, is]
                const float py = (1 - 1. / is) * ((2. / (is - 1)) * pyi - 1);   // coordinate of y-position [-1, 1]
                const float px = (1 - 1. / is) * ((2. / (is - 1)) * pxi - 1);   // coordinate of x-position [-1, 1]

                const int pfn = face_index_map[pi];        // face number of current position
                const float pp = images[pi];                // pixel intensity of current position

                for (int axis = 0; axis < 2; axis++) {
                    for (int direction = -1; direction <= 1; direction += 2) {
                        int qfn_last = pfn;
                        for (int d_pq = 1; d_pq < is; d_pq++) {
                            int qxi, qyi;
                            float qx, qy;
                            if (axis == 0) {
                                qxi = pxi + direction * d_pq;
                                qyi = pyi;
                                qx = (1 - 1. / is) * ((2. / (is - 1)) * qxi - 1);
                                qy = py;
                                if (qxi < 0 || is <= qxi) break;
                            } else {
                                qxi = pxi;
                                qyi = pyi + direction * d_pq;
                                qx = px;
                                qy = (1 - 1. / is) * ((2. / (is - 1)) * qyi - 1);
                                if (qyi < 0 || is <= qyi) break;
                            }

                            const int qi = bn * is2 + qyi * is + qxi;
                            const float qp = images[qi];
                            const float diff = qp - pp;
                            const int qfn = face_index_map[qi];

                            if (diff == 0) continue;                    // continue if same pixel value
                            if (0 <= diff * grad_pixel_sum) continue;   // continue if wrong gradient
                            if (qfn == qfn_last) continue;              // continue if p & q are on same face

                            /* adjacent point to check edge */
                            int rxi, ryi;
                            float rx, ry;
                            if (axis == 0) {
                                rxi = qxi - direction;
                                ryi = pyi;
                                rx = (1 - 1. / is) * ((2. / (is - 1)) * rxi - 1);
                                ry = py;
                            } else {
                                rxi = pxi;
                                ryi = qyi - direction;
                                rx = px;
                                ry = (1 - 1. / is) * ((2. / (is - 1)) * ryi - 1);
                            }

                            for (int mode = 0; mode < 2; mode++) {
                                float* face;
                                float* grad_face;
                                if (mode == 0) {
                                    if (qfn < 0) continue;
                                    face = &faces[(bn * nf + qfn) * 3 * 3];
                                    grad_face = &grad_faces[(bn * nf + qfn) * 3 * 3];
                                } else if (mode == 1) {
                                    if (qfn_last != pfn) continue;
                                    if (pfn < 0) continue;
                                    face = &faces[(bn * nf + pfn) * 3 * 3];
                                    grad_face = &grad_faces[(bn * nf + pfn) * 3 * 3];
                                }

                                /* for each edge */
                                for (int vi0 = 0; vi0 < 3; vi0++) {
                                    /* get vertices */
                                    int vi1 = (vi0 + 1) % 3;
                                    float* v0 = &face[vi0 * 3];
                                    float* v1 = &face[vi1 * 3];

                                    /* get cross point */
                                    float sx, sy;
                                    if (axis == 0) {
                                        sx = (py - v0[1]) * (v1[0] - v0[0]) / (v1[1] - v0[1]) + v0[0];
                                        sy = py;
                                    } else {
                                        sx = px;
                                        sy = (px - v0[0]) * (v1[1] - v0[1]) / (v1[0] - v0[0]) + v0[1];
                                    }

                                    /* continue if not cross edge */
                                    if ((rx < sx) != (sx < qx)) continue;
                                    if ((ry < sy) != (sy < qy)) continue;
                                    if ((v0[1] < sy) != (sy < v1[1])) continue;
                                    if ((v0[0] < sx) != (sx < v1[0])) continue;

                                    /* signed distance (positive if pi < qi) */
                                    float dist_v0, dist_v1;
                                    if (axis == 0) {
                                        dist_v0 = (px - sx) * (v1[1] - v0[1]) / (v1[1] - py);
                                        dist_v1 = (px - sx) * (v0[1] - v1[1]) / (v0[1] - py);
                                    } else {
                                        dist_v0 = (py - sy) * (v1[0] - v0[0]) / (v1[0] - px);
                                        dist_v1 = (py - sy) * (v0[0] - v1[0]) / (v0[0] - px);
                                    }

                                    /* add small epsilon */
                                    dist_v0 = (0 < dist_v0) ? dist_v0 + eps : dist_v0 - eps;
                                    dist_v1 = (0 < dist_v1) ? dist_v1 + eps : dist_v1 - eps;

                                    /* gradient */
                                    const float g_v0 = grad_pixel_sum * diff / dist_v0;
                                    const float g_v1 = grad_pixel_sum * diff / dist_v1;

                                    atomicAdd(&grad_face[vi0 * 3 + axis], g_v0);
                                    atomicAdd(&grad_face[vi1 * 3 + axis], g_v1);
                                }
                            }
                            qfn_last = qfn;
                        }
                    }
                }
            ''',
            'function',
        )(xp.arange(bs * is_ * is_).astype('int32'), faces, self.face_index_map, self.images, grad_images.ravel(),
          is_, nf, self.eps, grad_faces)

        return grad_faces, grad_textures

    def forward_cpu(self, inputs):
        raise NotImplementedError

    def backward_cpu(self, inputs, grad_outputs):
        raise NotImplementedError


def rasterize(faces, textures, image_size=256, anti_aliasing=True, near=0.1, far=100, eps=1e-3, background_color=(0, 0, 0)):
    if anti_aliasing:
        images = Rasterize(image_size * 2, near, far, eps, background_color)(faces, textures)
        images = images.transpose((0, 3, 1, 2))
        images = cf.average_pooling_2d(images, 2, 2)
    else:
        images = Rasterize(image_size, near, far, eps, background_color)(faces, textures)
        images = images.transpose((0, 3, 1, 2))
    images = images[:, :, ::-1, :]
    return images
