"""
Mason: a fast library to read Tiled TMX files.

For Python 3.3+

* TMX and JSON parsing
* Embedded images are supported
* Supports many features up to version 1.4.0

Mason is designed to read Tiled TMX files and prepare them for easy use for games.

This file uses a template to generate the library for map loading.
"""
import array
import logging
import os
import struct
from collections import deque, namedtuple
from itertools import product
from unittest import TestCase

__version__ = (3, 22, 0)
tiled_version = "1.4.2"

logger = logging.getLogger(__name__)

# internal flags
TRANS_FLIPX = 1
TRANS_FLIPY = 2
TRANS_ROT = 4

# Tiled gid flags
GID_TRANS_FLIPX = 1 << 31
GID_TRANS_FLIPY = 1 << 30
GID_TRANS_ROT = 1 << 29

flag_names = (
    "flipped_horizontally",
    "flipped_vertically",
    "flipped_diagonally",
)

AnimationFrame = namedtuple("AnimationFrame", ("gid", "duration"))
Attr = namedtuple("Attribute", ("name", "cls", "default", "comment"))
Child = namedtuple("ChildType", ("name", "cls"))
TileFlags = namedtuple("TileFlags", flag_names)

Color = Attr("color", str, None, "color of the thing")
Opacity = Attr("opacity", float, 1.0, "opacity")
Visible = Attr("visible", bool, True, "visible, or not")


class UnsupportedFeature(Exception):
    pass


# casting for properties types
tiled_property_type = {
    "string": str,
    "int": int,
    "float": float,
    "bool": bool,
    "color": str,
    "file": str,
}


def default_image_loader(filename, flags, **kwargs):
    """This default image loader just returns filename,
    rect, and any flags
    """

    def load(rect=None, flags=None):
        return filename, rect, flags

    return load


def noop(arg):
    return arg


def decompress_zlib(data):
    import zlib

    return zlib.decompress(data)


def decompress_gzip(data):
    import gzip
    import io

    with gzip.GzipFile(fileobj=io.BytesIO(data)) as fh:
        return fh.read()


def decode_base64(data):
    from base64 import b64decode

    return b64decode(data.strip())


def decode_csv(data):
    return map(int, "".join(i.strip() for i in data.strip()).split(","))


def get_data_xform(prefix, exception):
    """Generic function to transform data and raise exception"""

    def func(data, xform):
        if xform:
            try:
                xformer = globals()[prefix + xform]
            except KeyError:
                raise exception(xform)
            return xformer(data)

    return func


decompress = get_data_xform("decompress_", UnsupportedFeature)
decode = get_data_xform("decode_", UnsupportedFeature)


def unpack(data, encoding, compression):
    """Decode and decompress level tile data"""
    for func, arg in [(decode, encoding), (decompress, compression)]:
        temp = func(data, arg)
        if temp is not None:
            data = temp
    return data


def unroll_layer_data(data):
    fmt = struct.Struct("<L")
    every_4 = range(0, len(data), 4)
    return [decode_gid(fmt.unpack_from(data, i)[0]) for i in every_4]


def rowify(gids, w, h):
    return tuple(array.array("H", gids[i * w : i * w + w]) for i in range(h))


def read_points(text):
    """Parse a text string of float tuples and return [(x,...),...]"""
    return tuple(tuple(map(float, i.split(","))) for i in text.split())


def move_points(points, x, y):
    """Given list of points, return new one offset by (x, y)"""
    return tuple((i[0] + x, i[1] + y) for i in points)


def calc_bounds(points):
    """Given list of points, return mix/max of each axis"""
    x1 = x2 = y1 = y2 = 0
    for x, y in points:
        if x < x1:
            x1 = x
        elif x > x2:
            x2 = x
        if y < y1:
            y1 = y
        elif y > y2:
            y2 = y
    return abs(x1) + abs(x2), abs(y1) + abs(y2)


def decode_gid(raw_gid):
    """Decode a GID from TMX data

    as of Tiled 0.7.0 tile can be flipped when rendered
    as of Tiled 0.8.0 bit 30 determines if GID is rotated
    """
    flags = TileFlags(
        raw_gid & GID_TRANS_FLIPX == GID_TRANS_FLIPX,
        raw_gid & GID_TRANS_FLIPY == GID_TRANS_FLIPY,
        raw_gid & GID_TRANS_ROT == GID_TRANS_ROT,
    )
    gid = raw_gid & ~(GID_TRANS_FLIPX | GID_TRANS_FLIPY | GID_TRANS_ROT)
    return gid


class Token:
    """Tokens store and modify data found in Tiled map objects

    Tokens allow abstraction of data format (xml/json) and to
    define relationships between node types.

    Data in the Tokens are used to generate pytmx.py, so that
    code is not duplicated across loader and utility functions.

    * Attributes are used to generate pytmx.py
    * Tokens are nodes of a graph
    * Define behavior when adding children with "add_" methods
    """

    childtypes = []
    attributes = []

    def __init__(self):
        self.attrib = dict()
        self.properties = dict()

        attr = getattr(self, "attributes", [])
        self.attrib_types = {item.name: item for item in attr}

        for child in self.childtypes:
            setattr(self, child.name, child.cls())

    def __getattr__(self, name):
        try:
            return self.attrib[name]
        except KeyError:
            raise AttributeError

    def as_object(self):
        """Return representation of object as a generic python type

        :return:
        """
        obj = dict()
        obj.update(self.attrib)
        obj["properties"] = self.properties
        return obj

    def start(self, init, context):
        """Called when data is parsed from the source file

        The content of the source file may not be ready,
        but attributes (inside of xml tag) will be available.

        Once `Token.end` is called, the node is considered
        complete and will be added to graph.
        """
        attrib = dict()
        self.attrib = attrib

        # check the defaults
        for key, value in self.attrib_types.items():
            try:
                raw_value = init[key]
            except KeyError:
                raw_value = value.default

            attrib[key] = raw_value

        # cast values to their type
        for key, value in attrib.items():
            if value is not None:
                attrib[key] = self.attrib_types[key].cls(value)

    def end(self, content, context):
        """Called when content of source data is available

        After this method ends, all data transformation must be done
        and all data in `content` will be lost.

        :param content: Data contained in the tag of source file
        :param context: Useful data about graph, shared across nodes
        """
        pass

    def combine(self, child, tag):
        """Add child to this token in a meaningful way

        This method will attempt to call another method of this
        call named "add_ + tag".

        For example, if the tag is "image", this method will attempt to
        call Token.add_image().  An exception is raised if the method is not
        available.
        """
        try:
            func = getattr(self, "add_" + tag)
        except AttributeError:
            raise UnsupportedFeature(self.__class__.__name__, tag)
        func(child)

    def add_properties(self, item):
        self.properties = item.dictionary


class AnimationToken(Token):
    def __init__(self):
        super(AnimationToken, self).__init__()
        self.frames = list()

    def add_frame(self, item):
        self.frames.append(item)


class ChunkToken(Token):
    attributes = (
        Attr("x", int, None, "x tile coord of chunk"),
        Attr("y", int, None, "y tile coord of chunk"),
        Attr("width", int, None, "tile width of chunk"),
        Attr("height", int, None, "x tile height of chunk"),
    )

    def add_tile(self, item):
        raise UnsupportedFeature


class DataToken(Token):
    attributes = (
        Attr("encoding", str, None, "base64, csv, None"),
        Attr(
            "compression",
            str,
            None,
            "gzip, zip, None",
        ),
    )

    def __init__(self):
        super(DataToken, self).__init__()
        self.tiles = list()
        self.chunks = list()
        self.data = None

    def end(self, content, context):
        # get map dimension info from the parent
        parent = context["parent"]
        w, h = parent.width, parent.height

        # the content must be stripped before testing because
        # it may be a just a bunch of whitespace.
        # only the content will contain encoded/compressed tile data.
        if content.strip():
            # 1. unpack into list of 32-bit integers
            # 2. split data into several arrays, one per row
            data = unpack(content, self.encoding, self.compression)
            self.data = rowify(unroll_layer_data(data), w, h)

        # if for some reason tile elements are used
        elif self.tiles:
            self.data = rowify([i.gid for i in self.tiles], w, h)

        else:
            raise Exception("no layer data?")

    def add_tile(self, item):
        self.tiles.append(item)

    def add_chunk(self, item):
        self.chunks.append(item)


class EllipseToken(Token):
    pass


class FrameToken(Token):
    attributes = (
        Attr("tileid", int, None, "local id within parent tileset"),
        Attr("duration", int, None, "duration in milliseconds"),
    )


class GridToken(Token):
    attributes = (
        Attr("name", str, None, "name of group"),
        Attr("offsetx", int, 0, "pixel offset, applied to all descendants"),
        Attr("offsety", int, 0, "pixel offset, applied to all descendants"),
        Visible,
        Opacity,
    )


class GroupToken(Token):
    attributes = (
        Attr("name", str, None, "name of group"),
        Attr("offsetx", int, 0, "pixel offset, applied to all descendants"),
        Attr("offsety", int, 0, "pixel offset, applied to all descendants"),
        Visible,
        Opacity,
    )

    def add_layer(self, item):
        raise UnsupportedFeature

    def add_objectgroup(self, item):
        raise UnsupportedFeature

    def add_imagelayer(self, item):
        raise UnsupportedFeature

    def add_group(self, item):
        raise UnsupportedFeature


class ImagelayerToken(Token):
    attributes = (
        Attr("name", str, "ImageLayer", "name of layer"),
        Attr("offsetx", int, 0, "not used, per spec."),
        Attr("offsety", int, 0, "not used, per spec."),
        Visible,
        Opacity,
    )

    def __init__(self):
        super(ImagelayerToken, self).__init__()
        self.image = None

    def add_image(self, item):
        self.image = item


class ImageToken(Token):
    attributes = (
        Attr("format", str, None, "png, jpg, etc"),
        Attr("source", str, None, "path, relative to the map"),
        Attr("trans", str, None, "transparent color"),
        Attr("width", int, None, "pixel width, optional"),
        Attr("height", int, None, "pixel height, optional"),
    )

    def end(self, content, context):
        loader_class = context["image_loader"]
        loader = loader_class(self.source, None)
        self.image = loader()

    def add_data(self, item):
        # data is used to load image into memory.  uses ImageToken.format
        raise UnsupportedFeature


class LayerToken(Token):
    attributes = (
        Attr("name", str, "TileLayer", "name of layer"),
        Attr("width", int, None, "tile width"),
        Attr("height", int, None, "tile height"),
        Attr("offsetx", int, 0, "Not used, per spec"),
        Attr("offsety", int, 0, "Not used, per spec"),
        Visible,
        Opacity,
    )

    def __init__(self):
        super(LayerToken, self).__init__()
        self.data = None

    def add_data(self, data):
        self.data = data


class MapToken(Token):
    attributes = (
        Attr("version", str, None, "TMX format version"),
        Attr("tiledversion", str, None, "software version"),
        Attr("orientation", str, "orthogonal", "map orientation"),
        Attr("renderorder", str, "right-down", "order of tiles to be drawn"),
        Attr(
            "compressionlevel",
            int,
            -1,
            "The compression level to use for tile layer data",
        ),
        Attr("width", int, None, "map width in tiles"),
        Attr("height", int, None, "map height in tiles"),
        Attr("tilewidth", int, None, "pixel width of tile"),
        Attr("tileheight", int, None, "pixel height of tile"),
        Attr("hexsidelength", float, None, "[hex map] length of hex tile edge"),
        Attr("staggeraxis", str, None, "[hex map] x/y axis is staggered"),
        Attr("staggerindex", str, None, "[hex map] even/odd staggered axis"),
        Attr("backgroundcolor", str, None, "background color of map"),
        Attr("nextlayerid", int, None, "ID of the next layer"),
        Attr("nextobjectid", int, None, "the next gid available to use"),
        Attr("infinite", bool, False, "infinite map"),
    )
    childtypes = (
        Child("tilesets", list),
        Child("layers", list),
        Child("objectgroups", list),
        Child("group", list),
        Child("editorsettings", dict),
    )

    def __init__(self):
        super(MapToken, self).__init__()
        self.tilesets = list()
        self.layers = list()
        self.objectgroups = list()

    def as_object(self):
        obj = super(MapToken, self).as_object()
        for name in "tilesets layers objectgroups".split():
            r = list()
            obj[name] = r
            o = getattr(self, name)
            for thing in o:
                r.append(thing.as_object())

        return obj

    def add_tileset(self, item):
        self.tilesets.append(item)

    def add_layer(self, item):
        self.layers.append(item)

    def add_objectgroup(self, item):
        self.objectgroups.append(item)

    def add_imagelayer(self, item):
        self.layers.append(item)

    def add_group(self, item):
        raise UnsupportedFeature


class ObjectgroupToken(Token):
    attributes = (
        Attr("name", str, None, "name of group"),
        Attr("x", float, 0, "not used, per spec"),
        Attr("y", float, 0, "not used, per spec"),
        Attr("width", int, None, "not used, per spec"),
        Attr("height", int, None, "not used, per spec"),
        Color,
        Visible,
        Opacity,
    )

    def __init__(self):
        super(ObjectgroupToken, self).__init__()
        self.objects = list()

    def add_object(self, item):
        self.objects.append(item)


class ObjectToken(Token):
    attributes = (
        Attr("name", str, None, "name of object"),
        Attr("id", int, None, "unique id assigned to object"),
        Attr("type", str, None, "defined by editor"),
        Attr("x", float, None, "tile x coordinate"),
        Attr("y", float, None, "tile y coordinate"),
        Attr("width", float, None, "pixel width"),
        Attr("height", float, None, "pixel height"),
        Attr("rotation", float, 0, "rotation"),
        Attr("gid", int, None, "reference a tile id"),
        Attr("template", str, None, "path, optional"),
        Visible,
        Opacity,
    )

    def __init__(self):
        super(ObjectToken, self).__init__()
        self.points = list()
        self.ellipse = False

    def add_ellipse(self, item):
        self.ellipse = True

    def add_polygon(self, item):
        self.points = move_points(item.points, self.x, self.y)
        self.attrib["closed"] = True

    def add_polyline(self, item):
        self.points = move_points(item.points, self.x, self.y)
        self.attrib["closed"] = False

    def add_text(self, item):
        raise UnsupportedFeature

    def add_image(self, item):
        raise UnsupportedFeature


class PointToken(Token):
    """ No attributes defined """

    pass


class PolygonToken(Token):
    attributes = (Attr("points", read_points, None, "coordinates of the polygon"),)


class PolylineToken(Token):
    attributes = (Attr("points", read_points, None, "coordinates of the polyline"),)


class PropertiesToken(Token):
    def __init__(self):
        super(PropertiesToken, self).__init__()
        self.dictionary = dict()

    def add_property(self, item):
        self.dictionary[item.name] = item.value


class PropertyToken(Token):
    attributes = (
        Attr("type", noop, None, "type of the property"),
        Attr("name", noop, None, "name of property"),
        Attr("value", noop, None, "value"),
    )

    def __init__(self):
        super(PropertyToken, self).__init__()

    def start(self, init, context):
        super(PropertyToken, self).start(init, context)
        try:
            _type = tiled_property_type[init["type"]]
            self.attrib["value"] = _type(init["value"])
        except KeyError:
            self.attrib["value"] = init["value"]


class TemplateToken(Token):
    def __init__(self):
        super(TemplateToken, self).__init__()
        self.tilesets = list()
        self.objects = list()

    def add_terrain(self, item):
        self.terrains.append(item)

    def add_object(self, item):
        self.objects.append(item)


class TerrainToken(Token):
    Attributes = {
        Attr("name", str, "", "name of terrain"),
        Attr("tile", int, 0, "local tile-id that represents terrain visually"),
    }


class TerraintypesToken(Token):
    def __init__(self):
        super(TerraintypesToken, self).__init__()
        self.terrains = list()

    def add_terrain(self, item):
        self.terrains.append(item)


class TextToken(Token):
    Attributes = (
        Attr("fontfamily", str, "sans-serif", "font family used"),
        Attr("pixelsize", int, 16, "size of font in pixels"),
        Attr("wrap", bool, False, "word wrap"),
        Attr("color", str, "#000000", "color of text"),
        Attr("bold", bool, False, "bold?"),
        Attr("italic", bool, False, "italic?"),
        Attr("underline", bool, False, "underline?"),
        Attr("strikeout", bool, False, "strikeout?"),
        Attr("kerning", bool, False, "render kerning, or not"),
        Attr("halign", str, "left", "horizontal alignment in object"),
        Attr("valign", str, "top", "vertical alignment in object"),
    )


class TileOffsetToken(Token):
    attributes = (
        Attr("x", int, None, "horizontal (left) tile offset"),
        Attr("y", int, None, "vertical (down) tile offset"),
    )


class TilesetSourceToken(Token):
    def end(self, content, context):
        if source[-4:].lower() == ".tsx":

            # external tilesets don"t save this, store it for later
            self.firstgid = int(content.get("firstgid"))

            # we need to mangle the path - tiled stores relative paths
            dirname = os.path.dirname(self.parent.filename)
            path = os.path.abspath(os.path.join(dirname, source))
            try:
                content = ElementTree.parse(path).getroot()
            except IOError:
                msg = "Cannot load external tileset: {0}"
                logger.error(msg.format(path))
                raise Exception

        else:
            msg = "Found external tileset, but cannot handle type: {0}"
            logger.error(msg.format(self.source))
            raise UnsupportedFeature(self.source)


class TilesetToken(Token):
    attributes = (
        Attr("firstgid", int, None, "first gid of tileset"),
        Attr("source", str, None, "filename of external data or None"),
        Attr("name", str, None, "name of tileset"),
        Attr("tilewidth", int, None, "max width in tiles"),
        Attr("tileheight", int, None, "max height in tiles"),
        Attr("spacing", int, 0, "pixels between each tile"),
        Attr("margin", int, 0, "pixels between tile and image edge"),
        Attr("tilecount", int, None, "number of tiles in tileset"),
        Attr("columns", int, None, "number of columns"),
        Attr("objectalignment", str, None, "allignment of tile objects"),
    )

    def __init__(self):
        super(TilesetToken, self).__init__()
        self.image = None
        self.tiles = list()

    def end(self, content, context):
        super(TilesetToken, self).end(content, context)
        if self.source is None:
            self.load_tiles(content, context)

    def load_tiles(self, content, context):
        tw, th = self.tilewidth, self.tileheight

        width = self.image.width
        height = self.image.height

        p = product(
            range(self.margin, height + self.margin - th + 1, th + self.spacing),
            range(self.margin, width + self.margin - tw + 1, tw + self.spacing),
        )

        path = self.image.source
        loader_class = context["image_loader"]
        loader = loader_class(path, None, colorkey=self.image.trans)

        # iterate through the tiles
        for gid, (y, x) in enumerate(p, self.firstgid):
            flags = None
            image = loader((x, y, tw, th), flags)

    def add_tileoffset(self, item):
        raise UnsupportedFeature

    def add_grid(self, item):
        raise UnsupportedFeature

    def add_image(self, item):
        self.image = item

    def add_terraintypes(self, item):
        raise UnsupportedFeature

    def add_tile(self, item):
        self.tiles.append(item)

    def add_wangsets(self, item):
        raise UnsupportedFeature


# idk
class TileToken(Token):
    # from tileset
    attributes = (
        Attr("id", int, None, "local id"),
        Attr("gid", int, None, "global id"),
        Attr("type", str, None, "defined in editor"),
        Attr("terrain", str, None, "optional"),
        Attr("probability", float, None, "optional"),
    )

    def __init__(self):
        super(TileToken, self).__init__()
        self.image = None

    def add_image(self, item):
        self.image = item

    def add_objectgroup(self, item):
        raise UnsupportedFeature

    def add_animation(self, item):
        raise UnsupportedFeature


def get_loader(path):
    name, ext = os.path.splitext(path.lower())
    try:
        func = globals()["load_" + ext[1:]]
        return func(path)
    except KeyError:
        raise UnsupportedFeature(ext)


def get_processor(feature):
    try:
        return globals()[feature + "Token"]()
    except KeyError:
        raise UnsupportedFeature(feature)


def load_json(path):
    """Import JSON data by emulating a sax api"""
    import json

    with open(path) as fp:
        root = json.load(fp)

    # TODO: handle resursion (only supports 1st level)

    yield "start", "Map", root, None
    for key, value in root.items():
        yield "start", key, value, None
        yield "end", key, value, None
    yield "end", "Map", root, None


def load_tmx(path):
    """Lazy-load XML data with sax"""
    # required for python versions < 3.3
    try:
        from xml.etree import cElementTree as ElementTree
    except ImportError:
        from xml.etree import ElementTree

    root = ElementTree.iterparse(path, events=("start", "end"))

    for event, element in root:
        yield event, element.tag.title(), element.attrib, element.text
        if event == "end":
            element.clear()


def write_codegen(name, func, fp):
    """Write generated code to a file"""
    attrib = globals()[name].attrib_types

    if func == "__init__":
        kwargs = sorted(attrib)
        kwargs_str = "".join([", {}=None".format(i) for i in kwargs])
        fp.write("    def __init__(self{}):\n\n".format(kwargs_str))

    elif func == "attributes":
        fp.write("        # Attributes, as of Tiled {}\n".format(tiled_version))
        for kwarg, info in sorted(attrib.items()):
            fp.write("        self.{0} = {0}  # {1}\n".format(kwarg, info.comment))


def write_pytmx(in_file, out_file):
    """Fill data into template"""
    for raw_line in in_file:
        line = raw_line.strip()
        if line.startswith("# codegen:"):
            head, tail = line.split(": ")
            name, func = tail.split(".")
            # write_codegen(name, func, out_file)
        else:
            out_file.write(raw_line)


def slurp(path):
    """Read a Tiled map file"""
    stack = deque([None])
    token = None

    context = {
        "image_loader": default_image_loader,
        "path_root": os.path.dirname(path),
        "parent": None,
        "stack": stack,
    }

    for event, tag, attrib, text in get_loader(path):
        if event == "start":
            token = get_processor(tag)
            token.start(attrib, context)
            stack.append(token)

        elif event == "end":
            token = stack.pop()
            parent = stack[-1]
            context["parent"] = parent
            token.end(text, context)
            if parent:
                parent.combine(token, tag.lower())

    import pprint

    pprint.pprint(token.as_object())
    # from pytmx import TiledMap
    # return TiledMap(token)


class TestCase2(TestCase):
    def test_init(self):
        import glob

        for filename in glob.glob("../apps/data/0.9.1/*tmx"):
            logger.info(filename)
            token = slurp(filename)
            # pprint.pprint((token, token.properties))

        # fill out the template with generated code
        # with open("pytmx_template.py") as in_file:
        #     with open("pytmx.py", "w") as out_file:
        #         write_pytmx(in_file, out_file)
