"""
PaddleOCR-VL 모델 로딩/페이지 이미지 추론/결과 정리를 담당하는 모듈이다.
BGR 이미지 배열을 입력받아 모델 원본 JSON과 공통 블록 목록을 반환한다.
추론 결과 변환 과정에서 발생한 오류와 처리 시간을 기록한다.

run.py가 이 모듈을 호출한다.
반환한 블록의 PDF 좌표 변환은 pdf_parser.py가 담당하고,
결과 저장과 bbox 시각화는 별도 모듈에서 수행한다.
모델 초기화 시 가중치 다운로드와 캐시 생성이 발생할 수 있다.

추론·JSON 변환·블록 정리·오류·시간 기록은 model_result.py에 위임한다.
설정 딕셔너리는 run.py의 실행 기록과 모델 초기화에 함께 사용한다.

설정 근거: PaddleOCR v3.7.0 생성자·predict와 공식 사용법.
https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/paddleocr/_pipelines/paddleocr_vl.py
https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html
"""

# NumPy(별칭 np)는 pdf_parser.py가 만든 uint8 BGR 배열의 타입을 표현한다.
import numpy as np

# 내부 결과 모듈: 모델 출력의 JSON 변환·영역 검증·실패 기록을 맡는다.
# json_builtin도 이 모듈에서 가져올 수 있도록 기존 공개 이름을 유지한다.
from .model_result import json_builtin, normalize_result_blocks, predict_page


def parser_options(device: str, cpu_threads: int = 4) -> dict:
    """모델을 로딩하지 않고 초기화 설정을 구성한다. 실행 기록에도 사용한다."""
    if not isinstance(device, str) or not device.strip():
        raise ValueError("device에 'cpu', 'gpu:0' 등의 장치를 지정해야 합니다.")
    if type(cpu_threads) is not int or cpu_threads < 1:
        raise ValueError("cpu_threads는 양의 정수여야 합니다.")

    return {
        # 공식 API가 지원하는 버전을 명시해 향후 기본 버전 변경을 피한다.
        # v1.6의 우열을 이 프로젝트에서 측정했다는 뜻은 아니다.
        "pipeline_version": "v1.6",

        # 각 모델에 맞는 Paddle 정적/동적 실행 경로를 사용한다.
        # 선정한 실행 프레임워크는 PaddlePaddle이다. OCR 모델 이름과 구분한다.
        "engine": "paddle",
        "device": device,

        # 전달받은 개인 컨테이너의 4코어에 맞춘 초기값이다.
        # 전체 서버의 코어 수를 가정하지 않으며 CLI로 바꿀 수 있다.
        # 이 값 하나로 모든 라이브러리의 스레드 수가 제한되지는 않는다.
        "cpu_threads": cpu_threads,

        # 외부 VLM 서버가 확인되지 않아 로컬 native 경로를 사용한다.
        # 개인 컨테이너에서 실행 가능한 속도·메모리인지 별도 측정해야 한다.
        "vl_rec_backend": "native",

        # 영역별 내용·좌표가 필요하므로 페이지 전체를 단일 문자열로 받지 않는다.
        "use_layout_detection": True,

        # 공식 기능: 문서 방향 분류는 추가 회전, unwarping은 이미지 보정을 한다.
        # 프로젝트 판단: 현재 pixel_to_pdf_matrix는 렌더링 원점·배율·PDF 회전만
        # 되돌린다. 추가 회전의 역행렬이나 비선형 변형의 역매핑은 구현하지 않았다.
        # 여기서는 추가 방향 교정과 비선형 보정을 적용하지 않는다.
        # 따라서 입력 이미지와 bbox의 기준을 단순하게 유지하도록 둘 다 끈다.
        # 공식 반환 좌표가 추가 전처리 전으로 복원되는지는 활성화 시 다시 확인해야
        # 하며, 항상 좌표가 틀린다고 단정하는 설정은 아니다. 비선형 변형은 일반적으로
        # 6개 숫자의 아핀 행렬만으로 되돌릴 수 없다.
        # 품질 최적화 결론도 아니다. 잘못 돌아간 스캔·굴곡 문서는 불리할 수 있으므로
        # 원문과 비교해 별도 전처리 실험으로 다룬다. PDF 메타데이터 회전은 이미
        # pdf_parser.py의 렌더링에 반영되어 여기의 추가 방향 교정과 구분된다.
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,

        # 표본에 그래프가 있어 차트 인식을 시도한다. 값·축·범례의 정확성은
        # 검증되지 않았으므로 생성 내용을 정답 데이터처럼 사용하지 않는다.
        "use_chart_recognition": True,

        # 그림 영역에 글자가 포함된 표본을 다루므로 그림 내부 OCR도 시도한다.
        # 이 옵션이 모든 그림 속 문자를 복구한다는 보장은 없다.
        "use_ocr_for_image_block": True,

        # 도장(seal)은 문서에 찍힌 직인·인장이며, 여기서는 그 안의 글자를 뜻한다.
        # 이 옵션은 VL 파이프라인의 도장 영역 인식을 제어한다.
        # 근거: 설치된 PaddleX 3.7.2의 PaddleOCR-VL-1.6.yaml은 기본값이 False다.
        # 현재는 그 기본값을 유지한다. DATA_SPEC.md에 도장 관찰이 기록돼 있지 않지만
        # PDF에 도장이 없다고 확인한 것은 아니다. False의 품질상 이점도 미검증이다.
        # 도장 속 글자가 발견되면 같은 페이지에서 활성화 전후의 누락과 비용을 비교한다.
        # 공식 설정:
        # https://github.com/PaddlePaddle/PaddleX/blob/release/3.7/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml
        "use_seal_recognition": False,

        # 모델 출력에 프로젝트가 별도 Markdown 서식을 덧붙이지 않기 위한 설정이다.
        # 표의 HTML·수식 표현 자체를 일반 문장으로 바꾸거나 정제하지 않는다.
        "format_block_content": False,

        # 나중에 Markdown을 만들더라도 머리말·각주 등을 자동 제외하지 않도록 한다.
        # 현재 저장하는 원본 JSON에 없던 내용이 이 설정으로 생기지는 않는다.
        "markdown_ignore_labels": [],
        
        # 공식 기능: True이면 입력 읽기·레이아웃·VLM 단계를 큐와 여러 스레드로
        # 연결한다. 대량 입력의 처리량을 높일 수 있다(공식 문서 및 PaddleX 구현).
        # 프로젝트 판단: 현재 run.py는 직접 렌더링한 한 장을 전달하고 그 결과·오류를
        # 저장한 뒤 다음 장으로 진행한다. 단계별 겹침보다 페이지 단위 추적을 우선해
        # 내부 큐를 끈다. CPU 커널까지 단일 스레드로 만든다는 뜻은 아니다.
        # 큐 비활성화의 품질·메모리·속도 이점은 측정하지 않았다. 전체 PDF 처리량이
        # 병목이면 다중 이미지 입력과 함께 큐 유무·최대 메모리를 통제 비교한다.
        "use_queues": False,
    }


def build_parser(device: str, cpu_threads: int = 4):
    """명시한 설정으로 모델을 한 번 로딩한다. 실패는 run.py에 전달한다."""
    options = parser_options(device, cpu_threads)

    # PaddleOCR는 문서 분석 도구 모음이며 PaddleOCRVL은 그 안의 파이프라인이다.
    # 지연 import를 사용해 --help와 설정 조회만으로 모델 의존성을 로딩하지 않는다.
    from paddleocr import PaddleOCRVL

    return PaddleOCRVL(**options)


def normalize_blocks(raw: dict, page_id: str) -> list[dict]:
    """기존 호출부에서도 사용할 수 있는 VL 공통 블록 변환 함수다."""
    return normalize_result_blocks(raw, page_id, "paddleocr_vl", "vl")


def parse_image(parser, image: np.ndarray, page_id: str) -> dict:
    """원본 결과와 공통 블록을 반환한다. PDF 좌표는 run.py에서 추가한다."""
    # rect는 공식 predict 옵션이다. 현재 공통 bbox 스키마가 축 정렬 사각형이므로
    # 영역을 원본 PDF 위에 표시할 수 있도록 사각형 bbox로 받는다.
    # 휘어진 영역의 정밀한 윤곽을 그대로 보존하는 설정은 아니다.
    # 원본 JSON은 함께 보존해 추가로 반환된 모델 필드가 사라지지 않게 한다.
    return predict_page(
        parser,
        image,
        page_id,
        "paddleocr_vl",
        "vl",
        predict_options={"layout_shape_mode": "rect"},
    )

