"""파싱·실험 기록과 이후 RAG 단계가 함께 쓰는 도메인 무관 도구 패키지다.

files는 엄격한 JSON·해시·경로 경계를, redaction은 민감정보 제거를 맡는다.
의존 방향의 가장 아래에 있으므로 parsing·experiments를 import하지 않는다.
import만으로 파일을 만들거나 모델을 로딩하지 않는다.
"""
