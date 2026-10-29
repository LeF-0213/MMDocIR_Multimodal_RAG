"""
PDF에서 텍스트층 추출/페이지 렌더링/좌표 변환을 담당하는 모듈이다.
PDF bytes로 문서를 열고, 텍스트와 이미지 배치 정보를 추출한다.
페이지를 모델 입력 이미지로 만들고, 이미지 좌표를 PDF 좌표로 변환한다.

run.py가 이 모듈을 호출하고, 렌더링한 이미지를 v1_parser.py에 전달한다.
"""

# 표준 라이브러리 math로 NaN·무한대 좌표와 퇴화한 변환을 검사한다.
import math

# NumPy(별칭 np)로 렌더링 bytes를 모델 입력 배열로 바꾸고 숫자 타입을 검사한다.
import numpy as np

# PyMuPDF로 PDF의 텍스트·이미지 배치·페이지 회전을 읽고 같은 페이지를 렌더링한다.
# 추출과 렌더링에 같은 문서를 사용해 원문 위치를 연결한다.
import pymupdf


def error_info(exc: Exception) -> dict:
    """예외 종류와 메시지를 JSON에 저장할 수 있는 형태로 반환한다."""
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _finite_numbers(
    values: list | tuple,
    expected_length: int,
    field_name: str,
) -> list[float]:
    """길이와 숫자 형식을 검사하고 유한한 float 목록으로 반환한다."""
    if not isinstance(values, (list, tuple)) or len(values) != expected_length:
        raise ValueError(f"{field_name}: 숫자 {expected_length}개가 필요합니다.")

    converted = []

    for value in values:
        # bool은 int의 하위 타입이지만 좌표로 허용하지 않는다.
        # Numpy 숫자도 받을 수 있도록 해당 숫자 타입을 포함한다.
        if isinstance(value, bool) or not isinstance(
            value, (int, float, np.integer, np.floating)
        ):
            raise ValueError(f"{field_name}: 숫자가 아닌 값입니다: {value!r}")

        number = float(value)

        # Nan/무한대는 좌표 계산과 표준 JSON 저장에 사용할 수 없다.
        if not math.isfinite(number):
            raise ValueError(f"{field_name}: 유한한 숫자가 아닙니다: {value!r}")

        converted.append(number)

    return converted


def open_pdf(data: bytes) -> pymupdf.Document:
    """PDF bytes를 열어 Document를 반환한다."""
    if not isinstance(data, bytes):
        raise TypeError("PDF 입력은 bytes여야 합니다.")

    if not data:
        raise ValueError("PDF 입력이 비어 있습니다.")

    # stream은 파일 경로 대신 메모리의 bytes를 입력으로 받는다.
    # filetype은 입력을 PDF로 해석하도록 지정한다.
    # 손상되었거나 PDF가 아닌 입력의 오류는 호출부로 전달한다.
    document = pymupdf.open(
        stream=data,
        filetype="pdf",
    )

    if document.needs_pass:
        document.close()
        raise ValueError("암호가 필요한 PDF입니다.")

    return document


def prepare_page(
    page: pymupdf.Page,
    dpi: int,
) -> tuple[dict, np.ndarray | None, bytes | None]:
    """
    페이지 기록, BGR 이미지 배열, PNG bytes를 반환한다.

    텍스트층 추출·이미지 조회·렌더링의 실패를 각각 기록한다.
    렌더링이 실패하면 이미지 배열과 PNG는 None이다.
    page를 소유한 Document가 열린 상태에서 호출해야 한다.
    """
    # 잘못된 실행 설정은 페이지별 추출 실패로 숨기지 않는다.
    if type(dpi) is not int or dpi <= 0:
        raise ValueError("dpi는 0보다 큰 정수여야 합니다.")

    record = {
        "geometry": {
            # PyMuPDF의 회전 전 페이지 좌표
            # 원시 PDF의 좌하단 기준 좌표와 구분
            "coordinate_system": "pymupdf_unrotated_page_points",
            "origin": "top_left_of_unrotated_page",
            "unit": "point_1/72_inch",
            "rotation_degrees": page.rotation,

            # mediabox는 기본 페이지 영역
            # cropbox는 표시 영역이다.
            "mediabox": list(page.mediabox),
            "cropbox": list(page.cropbox),

            # page.rect는 PDF 페이지 회전이 반영된 페이지 영역이다.
            "rotated_page_rect": list(page.rect),
        },
    }

    try:
        # TEXTFLAGS_DICT는 dict 추출에 사용하는 기본 옵션 묶음이다.
        # TEXT_PRESERVE_IMAGES는 이미지 bytes를 결과에 포함하는 옵션이다.
        #
        # 이미지 포함 비트만 꺼서 큰 이미지 bytes가 텍스트 결과에 중복 저장되는 것을 피한다.
        # 이미지는 원본 PDF와 PNG로 보존한다.
        flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES

        # dict는 블록 -> 줄 -> span 구조와 좌표/글꼴 정보를 제공한다.
        # span은 글꼴 속성이 같은 텍스트 구간이다.
        #
        # sort=False로 라이브러리의 위치 기반 재정렬을 적용하지 않는다.
        # 다단 페이지의 올바른 읽기 순서를 보장하는 설정은 아니다.
        # 원래 추출 목록·좌표를 남겨 모델의 읽기 순서와 따로 비교하려는 선택이다.
        raw = page.get_text(
            "dict",
            flags=flags,
            sort=False,
        )

        # type=0인 텍스트 블록의 문자열을 줄 단위로 모은다.
        # 좌표와 원래 계층 구조는 raw로 별도로 유지한다.
        text = "\n".join(
            "".join(span["text"] for span in line["spans"])
            for block in raw["blocks"]
            if block["type"] == 0
            for line in block["lines"]
        )

        record["native"] = {
            # empty는 공백 이외의 텍스트가 없다는 뜻이다.
            # 이미지 속 글자의 존재 여부까지 판정한 결과는 아니다.
            "status": "ok" if text.strip() else "empty",
            "text": text,
            "raw": raw,
            "extraction_flags": flags,
            "sort": False,
        }

    except Exception as exc:
        # 텍스트층 추출 실패가 이미지 기반 파싱을 막지 않게 한다.
        record["native"] = {
            "status": "failed",
            "error": error_info(exc),
        }

    try:
        # 페이지에 배치된 이미지의 위치·크기·객체 번호 등을 조회한다.
        # 벡터 선이나 도형으로 그린 그림 전체를 찾는 기능은 아니다.
        #
        # hashes=True: 이미지 내용의 해시를 계산한다.
        # xrefs=True: PDF 내부 이미지 객체 번호를 조회한다.
        # 인라인 이미지 등은 xref가 0일 수 있다.
        # 해시 계산과 객체 번호 조회에는 추가 처리 시간이 든다.
        images = page.get_image_info(
            hashes=True,
            xrefs=True,
        )

        record["images"] = {
            "status": "ok",
            "options": {
                "hashes": True,
                "xrefs": True,
            },

            # PyMuPDF의 digest는 bytes이므로 JSON에 바로
            # 저장할 수 없다. 같은 값을 16진수 문자열로 표현한다.
            "digest_algorithm": "md5",
            "digest_encoding": "hex",
            "items": [
                # **item은 기존 필드의 새 dict에 복사한다.
                {
                    **item,
                    "digest": item["digest"].hex(),
                }
                for item in images
            ],
        }

    except Exception as exc:
        # 이미지 정보 조회가 실패해도 앞서 추출한 텍스트층은 유지한다.
        record["images"] = {
            "status": "failed",
            "error": error_info(exc),
        }

    try:
        # PDF는 72포인트가 1인치이다.
        # dpi/72 배율로 렌더링해 지정한 해상도의 이미지를 만든다.
        # DPI가 커지면 픽셀 수와 메모리/추론 비용이 증가한다.
        scale = pymupdf.Matrix(
            dpi / 72,
            dpi / 72,
        )

        # csRGB: RGB 색상으로 렌더링한다.
        # alpha=False: 투명도 채널을 제외해 3채널 입력을 만든다.
        # annots=True: PDF 주석/메모 표시를 렌더링에서 포함한다.
        # 원문 뷰어에서 보이는 주석도 비교 대상에 포함하기로 했으므로 켠다.
        # 추가된 메모가 OCR 결과에 들어갈 수 있어 실행 기록에도 설정을 남긴다.
        # PDF 자체의 페이지 회전은 렌더링에 반영된다.
        pix = page.get_pixmap(
            matrix=scale,
            colorspace=pymupdf.csRGB,
            alpha=False,
            annots=True,
        )

        #frombuffer는 이미지 bytes를 uint8 배열로 해석한다.
        # reshape는 이를 높이x너비xRGB 3채널 형태로 구성한다.
        rgb = np.frombuffer(
            pix.samples,
            dtype=np.uint8,
        ).reshape(
            pix.height,
            pix.width,
            3,
        )

        # PaddleOCR의 배열 입력에 맞춰 RGB를 BGR로 바꾼다.
        # 마지막 축의 ::-1은 채널 순서를 뒤집는다.
        # copy()는 모델 입력에 적합한 연속 메모리 배열을 만든다.
        image = rgb[:, :, ::-1].copy()

        # 이미지 픽셀 회전 전 PyMuPDF 페이지 좌표로 연결한다.
        """
        1. 렌더링 이미지의 원점 오프셋을 반영한다.
        2. 렌더링 배율을 되돌린다.
        3. PDF 페이지 회전을 되돌린다.
        """
        # 행렬에서 ~는 역변환(변환한 결과를 원래 상태로 되돌리는 것),
        # *는 변환 합성(두 개 이상의 변환을 순서대로 적용하여 하나의 변환으로 만드는 것)을 뜻한다.
        pixel_to_pdf = (
            pymupdf.Matrix(1, 0, 0, 1, pix.x, pix.y)
            * ~scale
            * page.derotation_matrix
        )

        record["render"] = {
            "status": "ok",
            "dpi": dpi,
            "width": pix.width,
            "height": pix.height,
            "coordinate_system": "rendered_page_image_pixels",
            "origin": "top_left",
            "unit": "pixel",
            "render_color_order": "RGB",
            "input_color_order": "BGR",
            "annotation_included": True,
            "pixmap_origin_pixels": [pix.x, pix.y],
            "pixel_to_pdf_matrix": list(pixel_to_pdf),
        }

        # PNG는 RGB 렌더링 결과에서 생성한다.
        # BGR 배열을 그대로 PNG로 저장하지 않는다.
        # 여기서는 bytes만 만들고 파일 저장은 저장 모듈에 맡긴다.
        png_bytes = pix.tobytes("png")

        return record, image, png_bytes

    except Exception as exc:
        record["render"] = {
            "status": "failed",
            "error": error_info(exc),
        }

        # 렌더링 실패 시에도 이미 확보한 텍스트/이미지 정보는 반환한다.
        return record, None, None


def add_pdf_coordinates(
    blocks: list[dict],
    matrix_values: list,
) -> list[dict]:
    """
    원래 블록을 수정하지 않고 PDF 좌표를 붙인 새 목록을 반환한다.

    bbox_image_pixels는 렌더링 이미지 기준의
    [x0, y0, x1, y1] 형식이어야 한다.

    모델이 추가 회전·왜곡 보정을 수행한 이미지의 좌표를 반환하면,
    해당 변환을 되돌린 뒤 이 함수에 전달해야 한다.
    """
    if not isinstance(blocks, list):
        raise TypeError("blocks는 목록이어야 합니다.")

    values = _finite_numbers(
        matrix_values,
        expected_length=6,
        field_name="pixel_to_pdf_matrix",
    )

    # 행렬의 선형 부분이 퇴화했는지 검사한다.
    # 행렬식이 0이면 영역이 선이나 점으로 눌리는 변환이 된다.
    a, b, c, d, _, _ = values
    determinant = a * d - b * c

    if not math.isfinite(determinant) or determinant == 0:
        raise ValueError("좌표 변환 행렬의 선형 부분이 유효하지 않습니다.")

    # *matrix_values는 저장한 여섯 숫자를 Matrix의 개별 인자로 전달한다.
    matrix = pymupdf.Matrix(*values)
    converted = []

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise TypeError(f"blocks[{index}]는 딕셔너리여야 합니다.")

        bbox = _finite_numbers(
            block.get("bbox_image_pixels"),
            expected_length=4,
            field_name=f"blocks[{index}].bbox_image_pixels",
        )
        x0, y0, x1, y1 = bbox

        # 좌표를 임의로 정렬하면 원래 결과의 이상을 숨길 수 있다.
        # 뒤집힌 영역은 명시적인 오류로 처리한다
        if x1 < x0 or y1 < y0:
            raise ValueError(f"blocks[{index}]의 bbox가 뒤집혀 있습니다: {bbox!r}")

        # 회전된 페이지에서는 네 꼭짓점을 모두 변환해야 한다.
        points = [
            pymupdf.Point(x, y) * matrix
            for x, y in (
                (x0, y0),
                (x1, y0),
                (x1, y1),
                (x0, y1),
            )
        ]

        if any(
            not math.isfinite(point.x) or not math.isfinite(point.y)
            for point in points
        ):
            raise ValueError(
                f"blocks[{index}]의 변환 결과에 "
                "NaN 또는 무한대 좌표가 있습니다."
            )

        xs = [point.x for point in points]
        ys = [point.y for point in points]

        converted.append(
            {
                # 기존 내용과 이미지 좌표를 유지한 새 딕셔너리를 만든다.
                **block,

                # 각 꼭짓점의 좌표 값을 목록으로 저장한다.
                # list[point]처럼 타입을 표현하는 문법과 구분한다.
                "polygon_pdf_points": [[point.x, point.y] for point in points],
                "bbox_pdf_points": [
                    min(xs),
                    min(ys),
                    max(xs),
                    max(ys),
                ],
            }
        )

    return converted
