from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Path, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.dependencies import get_report_service
from app.schemas import ReportDetails, ReportListResponse, ReportStatus, ReportSummary
from app.services.reports import ReportService

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
ReportServiceDependency = Annotated[ReportService, Depends(get_report_service)]


@router.post("", response_model=ReportSummary, status_code=status.HTTP_202_ACCEPTED)
def create_report(
    file: Annotated[UploadFile, File()],
    service: ReportServiceDependency,
) -> ReportSummary:
    try:
        report = service.register_original(file.file, file.filename)
    finally:
        file.file.close()
    return ReportSummary.model_validate(report)


@router.get("", response_model=ReportListResponse)
def list_reports(
    service: ReportServiceDependency,
    report_status: Annotated[ReportStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportListResponse:
    page = service.list_reports(status=report_status, limit=limit, offset=offset)
    return ReportListResponse.model_validate(page)


@router.get("/{report_id}", response_model=ReportDetails)
def get_report(
    service: ReportServiceDependency,
    report_id: Annotated[int, Path(gt=0)],
) -> ReportDetails:
    return ReportDetails.model_validate(service.get_report(report_id))


@router.get("/{report_id}/original")
def download_original(
    service: ReportServiceDependency,
    report_id: Annotated[int, Path(gt=0)],
) -> StreamingResponse:
    download = service.download_original(report_id)
    return StreamingResponse(
        download.chunks,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(download.report.original_filename),
            "Content-Length": str(download.report.size_bytes),
        },
    )


def content_disposition(filename: str) -> str:
    ascii_fallback = "".join(
        character if character.isascii() and (character.isalnum() or character in "._-") else "_"
        for character in filename
    )
    ascii_fallback = ascii_fallback or "original.csv"
    encoded_filename = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; ' f"filename*=UTF-8''{encoded_filename}"
