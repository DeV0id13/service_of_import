class ServiceError(Exception):
    status_code = 500
    code = "internal_error"
    public_message = "An internal error occurred"


class FileTooLargeError(ServiceError):
    status_code = 413
    code = "file_too_large"
    public_message = "The uploaded file exceeds the maximum allowed size"


class StorageUnavailableError(ServiceError):
    status_code = 503
    code = "storage_unavailable"
    public_message = "Object storage is temporarily unavailable"


class ObjectNotFoundError(StorageUnavailableError):
    code = "object_not_found"
    public_message = "The requested object was not found"


class ReportNotFoundError(ServiceError):
    status_code = 404
    code = "report_not_found"
    public_message = "Report not found"


class WarehouseNotFoundError(ServiceError):
    status_code = 404
    code = "warehouse_not_found"
    public_message = "Warehouse not found"


class ProductNotFoundError(ServiceError):
    status_code = 404
    code = "product_not_found"
    public_message = "Product not found"


class ReportPersistenceError(ServiceError):
    pass


class OriginalObjectMissingError(ServiceError):
    code = "original_object_missing"
    public_message = "The original report file is unavailable"
