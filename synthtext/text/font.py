import os
import os.path as osp
import pickle
import numpy as np

from pygame import freetype

from synthtext.config import load_cfg


class FontState(object):
    """
    Defines the random state of the font rendering  
    """
    def __init__(self):
        load_cfg(self)

        char_freq_path = osp.join(self.data_dir, 'models/char_freq.cp')
        font_model_path = osp.join(self.data_dir, 'models/font_px2pt.cp')

        # get character-frequencies in the English language:
        with open(char_freq_path, 'rb') as f:
            self.char_freq = pickle.load(f)

        # get the model to convert from pixel to font pt size:
        with open(font_model_path, 'rb') as f:
            self.font_model = pickle.load(f)

        # get the names of fonts to use:
        self.font_list = osp.join(self.data_dir, 'fonts/fontlist.txt')
        self.fonts = [
            os.path.join(self.data_dir, 'fonts', f.strip())
            for f in open(self.font_list)
        ]

    def init_font(self, fs):
        """
        Initializes a pygame font.
        FS : font-state sample
        """
        font = freetype.Font(fs['font'], size=fs['size'])
        font.underline = fs['underline']
        font.underline_adjustment = fs['underline_adjustment']
        font.strong = fs['strong']
        font.oblique = fs['oblique']
        font.strength = fs['strength']
        char_spacing = fs['char_spacing']
        font.antialiased = True
        font.origin = True
        return font

    def sample(self):
        """
        Samples from the font state distribution
        """
        debug = self.debug

        font_state = dict()

        idx = 0 if debug else int(np.random.randint(0, len(self.fonts)))
        font_state['font'] = self.fonts[idx]

        idx = 0 if debug else int(np.random.randint(0, len(self.capsmode)))
        font_state['capsmode'] = self.capsmode[idx]


        var = 0 if debug else np.random.randn() 
        font_state['size'] = self.size[1] * var  + self.size[0]

        var = 0 if debug else np.random.randn()
        font_state['underline_adjustment'] = max(2.0, min(-2.0, 
                self.underline_adjustment[1] * var +
                self.underline_adjustment[0]))

        var = 0 if debug else np.random.rand()
        font_state['strength'] = (self.strength[1] - self.strength[0]) * var + \
                self.strength[0]

        var = 0 if debug else np.random.beta(self.kerning[0], self.kerning[1])
        font_state['char_spacing'] = int(self.kerning[3] * var + self.kerning[2])

        flag = False if debug else np.random.rand() < self.underline
        font_state['underline'] = flag

        flag = False if debug else np.random.rand() < self.strong
        font_state['strong'] = flag

        flag = False if debug else np.random.rand() < self.oblique
        font_state['oblique'] = flag

        flag = False if debug else np.random.rand() < self.border
        font_state['border'] = flag

        flag = False if debug else np.random.rand() < self.random_caps
        font_state['random_caps'] = flag

        flag = False if debug else np.random.rand() < self.curved
        font_state['curved'] = flag

        font = self.init_font(font_state)
        return font

    def get_aspect_ratio(self, font, size=None):
        """
        Returns the median aspect ratio of each character of the font.
        """
        if size is None:
            size = 12  # doesn't matter as we take the RATIO
        chars = ''.join(self.char_freq.keys())
        w = np.array(self.char_freq.values())

        # get the [height,width] of each character:
        try:
            sizes = font.get_metrics(chars, size)
            good_idx = [i for i in range(len(sizes)) if sizes[i] is not None]
            sizes, w = [sizes[i] for i in good_idx], w[good_idx]
            sizes = np.array(sizes).astype('float')[:, [3, 4]]
            r = np.abs(sizes[:, 1] / sizes[:, 0])  # width/height
            good = np.isfinite(r)
            r = r[good]
            w = w[good]
            w /= np.sum(w)
            r_avg = np.sum(w * r)
            return r_avg
        except:
            return 1.0

    def get_font_size(self, font, font_size_px):
        """
        Returns the font-size which corresponds to FONT_SIZE_PX pixels font height.
        """
        m = self.font_model[font.name]
        return m[0] * font_size_px + m[1]  #linear model


class BaselineState(object):

    curve = lambda this, a: lambda x: a * x * x
    differential = lambda this, a: lambda x: 2 * a * x

    def __init__(self):
        load_cfg(self)

    def get_sample(self):
        """
        Returns the functions for the curve and differential for a and b
        """
        sgn = 1.0
        if np.random.rand() < self.p_sgn:
            sgn = -1

        a = self.a[1] * np.random.randn() + sgn * self.a[0]
        if self.debug:
            a = 0
        return {
            'curve': self.curve(a),
            'diff': self.differential(a),
        }


