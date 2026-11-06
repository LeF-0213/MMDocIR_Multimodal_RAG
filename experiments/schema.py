"""실험 기록·비교 파일의 이름과 스키마 버전을 정의한다.

record·compare·automatic·evaluate가 같은 저장 형식을 가리키도록 한 곳에 둔다.
값만 정의하며 파일을 읽거나 만들지 않는다.
JSON·해시·민감정보 처리는 도메인과 무관하므로 common 패키지가 맡는다.
"""

# 아래 이름은 실행 환경이 아닌 공통 파일 형식의 정의다.
MANIFEST_FILE = "experiment.json"
FINISHED_FILE = "experiment_finished.json"
RESOURCES_FILE = "resources.jsonl"
RECORD_SCHEMA = "project.experiment.v1"
COMPARE_SCHEMA = "project.experiment_comparison.v1"
GOLD_SCHEMA = "project.parsing_gold.v1"
