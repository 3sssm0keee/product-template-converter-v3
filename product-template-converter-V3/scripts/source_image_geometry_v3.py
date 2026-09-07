"""读取已绑定源文件中的图片显示尺寸，不改变内容决策或图片字节。"""
from math import hypot, isfinite
from pathlib import Path
import re


def pdf_image_display_sizes(source: Path) -> dict[tuple[int, int], tuple[float, float]]:
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    reader = PdfReader(source)
    sizes = {}

    def multiply(a, b):
        return (a[0]*b[0]+a[1]*b[2], a[0]*b[1]+a[1]*b[3],
                a[2]*b[0]+a[3]*b[2], a[2]*b[1]+a[3]*b[3],
                a[4]*b[0]+a[5]*b[2]+b[4], a[4]*b[1]+a[5]*b[3]+b[5])

    for page_index, page in enumerate(reader.pages, 1):
        by_object = {}
        user_unit = float(page.get('/UserUnit', 1))

        def walk(stream, resources, initial, depth=0):
            if depth > 16:
                return
            resources = resources.get_object() if hasattr(resources, 'get_object') else resources
            matrix = initial
            stack = []
            for operands, operator in ContentStream(stream, reader).operations:
                if operator == b'q':
                    stack.append(matrix)
                elif operator == b'Q':
                    matrix = stack.pop() if stack else initial
                elif operator == b'cm' and len(operands) == 6:
                    matrix = multiply(tuple(float(x) for x in operands), matrix)
                elif operator == b'Do' and operands:
                    objects = resources.get('/XObject', {})
                    objects = objects.get_object() if hasattr(objects, 'get_object') else objects
                    reference = objects.get(operands[0])
                    if reference is None:
                        continue
                    obj = reference.get_object()
                    if obj.get('/Subtype') == '/Image' and hasattr(reference, 'idnum'):
                        size = (hypot(matrix[0], matrix[1])*user_unit/72, hypot(matrix[2], matrix[3])*user_unit/72)
                        if all(isfinite(v) and v > 0 for v in size):
                            key = (reference.idnum, reference.generation)
                            prior = by_object.get(key)
                            if prior is None or size[0]*size[1] < prior[0]*prior[1]:
                                by_object[key] = size
                    elif obj.get('/Subtype') == '/Form':
                        form_matrix = tuple(float(x) for x in obj.get('/Matrix', [1, 0, 0, 1, 0, 0]))
                        if len(form_matrix) == 6:
                            walk(obj, obj.get('/Resources', resources), multiply(form_matrix, matrix), depth+1)
        contents = page.get_contents()
        if contents is not None:
            walk(contents, page.get('/Resources', {}), (1, 0, 0, 1, 0, 0))
        for image_index, image in enumerate(page.images, 1):
            ref = image.indirect_reference
            if ref is not None and (ref.idnum, ref.generation) in by_object:
                sizes[(page_index, image_index)] = by_object[(ref.idnum, ref.generation)]
    return sizes


def plan_image_display_sizes(plan: dict, owner: Path) -> dict[str, tuple[float, float]]:
    from pipeline_common import sha256_file
    from build_fixed_docx import load_images
    bound = plan.get('normalized_source') or {}
    if not bound:
        return {}
    raw = bound.get('path')
    expected = bound.get('sha256')
    if not isinstance(raw, str) or not isinstance(expected, str):
        raise ValueError('图片源尺寸缺少规范化源文件哈希绑定')
    source = Path(raw)
    source = source if source.is_absolute() else owner.parent / source
    if not source.is_file() or sha256_file(source) != expected.upper():
        raise ValueError('图片源尺寸的规范化源文件哈希失效')
    items = [{'id': item['source_id'], 'kind': 'image', 'location': item.get('source', {}).get('location', '')}
             for item in plan.get('items', []) if item.get('source', {}).get('payload', {}).get('type') == 'asset'
             and item.get('decision', {}).get('action') in {'preserve_image', 'preserve_sanitized_image'}]
    if not items:
        return {}
    if source.suffix.lower() == '.pdf':
        geometry = pdf_image_display_sizes(source)
        result = {}
        for item in items:
            match = re.fullmatch(r'page:(\d+)/image:(\d+)', item['location'])
            if match and (size := geometry.get((int(match[1]), int(match[2])))):
                result[item['id']] = size
        return result
    return {key: material.source_display_size for key, material in load_images({'items': items}, source).items()
            if material.source_display_size is not None}
