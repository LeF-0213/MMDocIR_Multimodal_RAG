"""
선택한 파서의 출력을 저장용 JSON과 영역 객체로 정리하는 내부 모듈이다.
vl_parser.py 또는 structure_parser.py가 한 페이지의 모델 결과를 전달하면
JSON 기본 타입·공통 영역·실패 단계·처리 시간을 반환한다.
원본 JSON은 따로 유지하고, 원본 라벨·내용·목록 순서를 임의로 교정하지 않는다.
PDF 좌표 변환은 pdf_parser.py, 파일 저장은 result_store.py의 책임이다.
이 모듈은 모델 로딩이나 파일 생성을 수행하지 않는다.

블록 내용의 <img src="imgs/..."> 참조가 가리키는 그림 crop은 JSON에 들어가지 않는다.
predict_page()는 그 그림을 JPEG bytes로 따로 반환하고, run.py가 꺼내 파일로 저장한다.
"""

# 표준 라이브러리: json은 직렬화 가능 여부, math는 좌표의 유한성을 검사한다.
# perf_counter는 시스템 시각 변경에 영향을 받지 않는 경과 시간을 측정한다.
# BytesIO는 그림을 파일 없이 메모리에서 인코딩하고, PurePosixPath는 라이브러리가
# 만든 상대 참조 경로가 결과 폴더를 벗어나지 않는지 운영체제와 무관하게 검사한다.
import json
import math
from io import BytesIO
from pathlib import PurePosixPath
from time import perf_counter

# NumPy(별칭 np)는 모델 입력 배열과 반환된 배열·숫자 타입을 처리한다.
import numpy as np

# Pillow(패키지 이름 PIL)의 Image는 PaddleX가 그림 crop을 담아 반환하는 이미지 객체다.
# 형식 확인과 JPEG 인코딩에만 사용한다. PaddleX가 이미 의존하는 라이브러리라 새 엔진을 들이지 않는다.
from PIL import Image

# PaddleX가 만든 참조 경로가 .jpg로 끝나므로 같은 형식으로 저장한다. JPEG는 손실 압축이다.
# 품질 값은 Pillow 기본값(75)과 같으며, PaddleX의 save_to_markdown 결과와 바이트가 같다는 보장은 없다.
RESULT_IMAGE_FORMAT = "JPEG"
RESULT_IMAGE_QUALITY = 75


def json_builtin(value):
    """NumPy 배열·스칼라를 JSON 기본 타입으로 바꾸되 알 수 없는 타입은 거부한다."""
    if isinstance(value, np.ndarray):
        return json_builtin(value.tolist())

    if isinstance(value, np.generic):
        return json_builtin(value.item())

    if isinstance(value, dict):
        # 숫자 키를 자동 문자열화하면 원본 키의 의미가 바뀔 수 있어 거부한다.
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON 객체의 키는 문자열이어야 합니다.")
        return {key: json_builtin(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [json_builtin(item) for item in value]

    if value is None or isinstance(value, (str, bool, int, float)):
        return value

    # default=str은 알 수 없는 객체의 구조를 잃으므로 사용하지 않는다.
    raise TypeError(f"지원하지 않는 JSON 타입: {type(value).__name__}")


def normalize_result_blocks(
    raw: dict,
    page_id: str,
    source: str,
    prefix: str,
) -> list[dict]:
    """v3.7.0 결과의 parsing_res_list를 변환한다. 불량 구조는 원본과 오류로 남긴다."""
    if not isinstance(raw, dict):
        raise TypeError("모델 결과는 객체여야 합니다.")
    if not isinstance(page_id, str) or not page_id.strip():
        raise ValueError("비어 있지 않은 page_id가 필요합니다.")

    # .json이 res로 감싼 형태도 허용하지만 원본 raw를 다시 쓰지는 않는다.
    payload = raw.get("res", raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("parsing_res_list"), list):
        raise TypeError("모델 결과에 parsing_res_list 목록이 필요합니다.")

    normalized = []

    for index, block in enumerate(payload["parsing_res_list"]):
        if not isinstance(block, dict):
            raise TypeError(f"영역 {index}: 객체가 아닙니다.")
        label, content = block.get("block_label"), block.get("block_content")
        if not isinstance(label, str) or not isinstance(content, str):
            raise TypeError(f"영역 {index}: block_label과 block_content는 문자열이어야 합니다.")
        bbox = block.get("block_bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"영역 {index}: block_bbox에 숫자 4개가 필요합니다.")
        if any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in bbox
        ):
            raise ValueError(f"영역 {index}: bbox에 유한한 숫자가 필요합니다.")
        x0, y0, x1, y1 = bbox
        if x1 < x0 or y1 < y0:
            raise ValueError(f"영역 {index}: bbox가 뒤집혀 있습니다.")
        normalized.append(
            {
                # 로컬 ID와 모델 ID는 분리한다. 공식 데이터셋 ID를 추정하지 않는다.
                "element_id": f"{page_id}:{prefix}:{index}",
                "id_origin": "project_generated",
                "page_id": page_id,
                "source": source,
                # 목록 순서와 모델의 읽기 순서 예측을 모두 보존한다.
                # block_order가 None인 영역을 임의로 정렬하지 않는다.
                "source_list_index": index,
                "model_block_id": block.get("block_id"),
                "model_block_order": block.get("block_order"),
                # 라이브러리의 병합 그룹 번호다. PaddleX 3.7.2 VL은 병합하지 않은 블록에
                # 자기 목록 순번을, 함께 인식한 블록에는 같은 번호를 넣는다.
                # 같은 번호의 블록은 내용이 첫 블록에 몰렸을 수 있어 청킹이 함께 다뤄야 한다.
                # 필드가 없는 결과는 None으로 두고 번호를 추정하지 않는다.
                "model_group_id": block.get("group_id"),
                "label": label,
                "content": content,
                "bbox_image_pixels": [float(v) for v in bbox],
            }
        )

    return normalized


def result_images(result) -> dict[str, bytes]:
    """결과 객체의 markdown_images를 {블록 내용의 참조 경로: JPEG bytes}로 반환한다.

    PaddleX 3.7.2의 markdown 속성은 그림 블록과 표 안에 들어간 그림 crop을
    상대 경로 → PIL 이미지로 제공한다. 병합된 VLM 입력 crop 전체를 주는 API는 아니다.
    """
    images = result.markdown.get("markdown_images") or {}
    if not isinstance(images, dict):
        raise TypeError("markdown_images는 경로→이미지 딕셔너리여야 합니다.")
    encoded = {}
    for reference, image in images.items():
        path = PurePosixPath(reference) if isinstance(reference, str) else None
        # 절대 경로·상위 폴더 이동·빈 조각이 있으면 저장 위치를 신뢰할 수 없어 전체를 거부한다.
        if (path is None or not reference or path.is_absolute() or "\\" in reference
                or any(part in ("", ".", "..") for part in reference.split("/"))):
            raise ValueError(f"그림 참조 경로가 상대 경로가 아닙니다: {reference!r}")
        # 배열이면 색 순서(RGB/BGR)를 확인할 근거가 없으므로 추정해 변환하지 않는다.
        if not isinstance(image, Image.Image):
            raise TypeError(f"{reference}: PIL 이미지가 아닙니다({type(image).__name__}).")
        buffer = BytesIO()
        # JPEG는 투명도·팔레트를 담지 못하므로 RGB로 바꿔 저장한다.
        image.convert("RGB").save(buffer, format=RESULT_IMAGE_FORMAT, quality=RESULT_IMAGE_QUALITY)
        encoded[reference] = buffer.getvalue()
    return encoded


def predict_page(
    parser,
    image: np.ndarray,
    page_id: str,
    source: str,
    prefix: str,
    predict_options: dict | None = None,
) -> dict:
    """한 장의 결과를 엄격히 검사하고 실패해도 확보한 raw를 유지한다."""
    started = perf_counter()
    output = {
        "page_id": page_id,
        "source": source,
        "status": "failed",
        "raw": None,
        "blocks": [],
        "block_count": None,
        "character_count": None,
    }

    stage = "input_validation"

    try:
        if not isinstance(page_id, str) or not page_id.strip():
            raise ValueError("비어 있지 않은 page_id가 필요합니다.")
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.ndim != 3
            or image.shape[2] != 3
            or image.shape[0] == 0
            or image.shape[1] == 0
        ):
            raise ValueError("이미지는 높이×너비×3 형태의 비어 있지 않은 uint8 BGR 배열이어야 합니다.")
        output["input_image"] = {
            "width": int(image.shape[1]),
            "height": int(image.shape[0]),
            "color_order": "BGR",
            "coordinate_system": "rendered_page_image_pixels",
        }

        stage = "prediction"
        # 모델 내부에서 입력 배열을 변경하더라도 호출부의 공통 입력은 보존한다.
        # C 순서 복사는 연속 메모리를 보장하며 페이지 한 장 분량의 추가 RAM을 쓴다.
        results = iter(
            parser.predict(
                input=image.copy(order="C"),
                **(predict_options or {}),
            )
        )
        sentinel = object()
        first = next(results, sentinel)
        if first is sentinel:
            raise ValueError("한 페이지 입력에서 결과가 반환되지 않았습니다.")
        if next(results, sentinel) is not sentinel:
            raise ValueError("한 페이지 입력에서 여러 결과가 반환되었습니다.")

        stage = "serialization"
        # v3.7.0 Result의 json은 메서드가 아닌 속성이다.
        raw = first.json
        if isinstance(raw, str):
            raw = json.loads(raw)
        raw = json_builtin(raw)
        if not isinstance(raw, dict):
            raise TypeError("모델 JSON의 최상위 값은 객체여야 합니다.")
        json.dumps(raw, ensure_ascii=False, allow_nan=False)
        output["raw"] = raw

        stage = "normalization"
        output["blocks"] = normalize_result_blocks(raw, page_id, source, prefix)
        output["block_count"] = len(output["blocks"])
        output["character_count"] = sum(
            len(b["content"].strip()) for b in output["blocks"]
        )
        # ok는 문자열 출력이 있다는 처리 상태다. HTML 태그·LaTeX도 길이에
        # 포함되므로 원문 문자 수·인식 정확도·누락 없음의 지표가 아니다.
        output["status"] = "ok" if output["character_count"] else "empty"

        # 그림 crop을 얻지 못해도 이미 확보한 문자 결과는 유효하므로 페이지 상태를 바꾸지 않는다.
        # 실패는 별도 상태로 남기고, 참조 파일 누락은 validate_run.py가 다시 검사한다.
        # result_image_bytes는 JSON에 저장할 수 없는 bytes라 run.py가 꺼낸 뒤 파일로 저장한다.
        try:
            output["result_image_bytes"] = result_images(first)
            output["result_images"] = {"status": "ok", "items": []}
        except Exception as exc:
            output["result_images"] = {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
    except Exception as exc:
        output["error"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        # 실패 경로도 측정해 성공 페이지 비용만 보고하는 편향을 피한다.
        output["elapsed_seconds"] = perf_counter() - started

    return output
