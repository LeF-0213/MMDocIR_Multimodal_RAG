"""
PP-StructureV3 비교 경로의 설정·모델 로딩·페이지 추론을 담당한다.
run.py가 전달한 BGR 페이지 이미지를 model_result.py의 저장 규칙으로 정리한다.
문자 OCR은 PP-OCRv6 medium, 수식 인식은 실행 시 지정한 모델을 사용한다.
모델 원본 출력과 영역을 반환하며 PDF 좌표 변환·저장은 run.py에 맡긴다.
모델 초기화 시 다운로드와 캐시 생성이 발생할 수 있다.
생성자 설정은 parser_options(), 페이지별 predict 설정은 predict_options()가 정하며
run.py는 같은 함수의 반환값을 run.json에 기록한다.

옵션 근거는 설치된 PaddleOCR 3.7.0의 PPStructureV3 생성자와 predict다.
https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/paddleocr/_pipelines/pp_structurev3.py
"""

# 표준 라이브러리 copy의 deepcopy는 중첩 옵션을 복사해 라이브러리의 수정이 다음 페이지에 번지지 않게 한다.
from copy import deepcopy

# NumPy(별칭 np)는 PDF 렌더링에서 받은 uint8 BGR 배열의 타입을 표현한다.
import numpy as np

# 내부 결과 모듈은 JSON 변환·영역 검증·오류·시간 기록을 맡는다.
# VL과 같은 형식으로 저장하되 source와 요소 ID 접두사는 구분한다.
from .model_result import predict_page


def parser_options(device: str, formula_model: str, cpu_threads: int = 4, *, overrides: dict | None = None) -> dict:
    """모델을 로딩하지 않고 실제 초기화와 실행 기록에 쓸 설정을 반환한다."""
    if not isinstance(device, str) or not device.strip():
        raise ValueError("device에 실행 장치를 지정해야 합니다.")
    if not isinstance(formula_model, str) or not formula_model.strip():
        raise ValueError("formula_model에 비교할 수식 모델 ID를 지정해야 합니다.")
    if type(cpu_threads) is not int or cpu_threads < 1:
        raise ValueError("cpu_threads는 양의 정수여야 합니다.")

    options = {
        # PaddlePaddle 실행 경로다. 프레임워크 이름을 인식 품질의 근거로 삼지 않는다.
        "engine": "paddle",
        "device": device,
        # 개인 컨테이너의 4코어를 초기 기준으로 삼으며 CLI에서 변경할 수 있다.
        "cpu_threads": cpu_threads,
        # Windows·PaddlePaddle 3.3.1·PaddleX 3.7.2의 실제 페이지 추론에서
        # oneDNN 경로가 pir::ArrayAttribute<pir::DoubleAttribute> 오류로 실패했다.
        # 설치된 PaddleOCR _common_args.py가 지원하는 옵션으로 oneDNN을 끄고
        # 일반 Paddle CPU 경로를 사용한다. 인식 품질 개선을 뜻하는 설정은 아니며
        # 가속을 끈 만큼 처리 시간이 늘어날 수 있다. 실행 기록에도 이 값을 남긴다.
        "enable_mkldnn": False,
        # 3.7.0 래퍼의 ocr_version은 v3·v4·v5만 허용한다.
        # v6를 그 옵션에 넣지 않고 설치된 구현이 받는 모델 이름을 직접 지정한다.
        "text_detection_model_name": "PP-OCRv6_medium_det",
        "text_recognition_model_name": "PP-OCRv6_medium_rec",
        # 수식 인식 모델은 문자 OCR과 별개다. 실행에서 선택한 ID를 기록한다.
        "formula_recognition_model_name": formula_model,
        "use_formula_recognition": True,
        # 선정 페이지의 표·차트 정보를 보존하려는 설정이며 정확성은 원본과 대조한다.
        "use_table_recognition": True,
        "use_chart_recognition": True,
        # 현재 PDF 역변환은 렌더링 원점·배율·PDF 회전만 되돌린다.
        # 추가 회전·비선형 보정의 역매핑이 없어 페이지 전처리는 적용하지 않는다.
        # 이 설정이 인식 품질에 가장 좋다고 측정한 것은 아니다.
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        # 잘라낸 텍스트 줄의 0·180도 방향을 판단해 OCR에 사용한다.
        # 페이지 전체 이미지나 검출 좌표를 회전시키는 옵션과 구분한다.
        # 설치된 PaddleX 3.7.2의 GeneralOCR 기본값을 유지한다.
        "use_textline_orientation": True,
        # 도장은 직인·인장 속 글자를 뜻한다. 설치된 파이프라인의 기본값을 유지한다.
        # 표본에 도장이 없다고 확인한 것은 아니며 발견 시 별도 검토가 필요하다.
        "use_seal_recognition": False,
        # 원본 라벨·내용을 비교할 수 있도록 추가 서식화와 Markdown 라벨 제외를 끈다.
        "format_block_content": False,
        "markdown_ignore_labels": [],
    }
    options.update(deepcopy(overrides or {}))
    return options


def predict_options() -> dict:
    """페이지마다 predict()에 넘길 설정이다. run.py는 같은 함수로 run.json에 기록한다."""
    # 표 셀 검출·OCR 결과 배정 같은 표 처리 옵션은 v3.7.0에서 predict 전용 인자다.
    # 현재는 아무것도 넘기지 않아 라이브러리 기본값을 사용한다.
    # 빈 딕셔너리는 "기본값을 확인했다"는 뜻이 아니며, 실제 기본값은 라이브러리 버전에 따른다.
    return {}


def build_parser(device: str, formula_model: str, cpu_threads: int = 4, *, overrides: dict | None = None):
    """명시한 설정으로 모델을 로딩한다. 초기화 실패는 실행부에 전달한다."""
    # PaddleOCR의 PPStructureV3는 레이아웃·OCR·표·수식 모델을 연결한다.
    # 지연 import로 도움말·설정 조회만으로 모델을 로딩하지 않게 한다.
    from paddleocr import PPStructureV3

    return PPStructureV3(**parser_options(device, formula_model, cpu_threads, overrides=overrides))


def parse_image(parser, image: np.ndarray, page_id: str, *, predict_overrides: dict | None = None) -> dict:
    """VL과 구분한 source·요소 ID로 원본 결과와 영역을 반환한다."""
    options = predict_options()
    options.update(deepcopy(predict_overrides or {}))
    return predict_page(
        parser,
        image,
        page_id,
        "pp_structure_v3",
        "structure",
        predict_options=options,
    )
