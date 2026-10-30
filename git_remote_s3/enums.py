# SPDX-FileCopyrightText: Amazon.com, Inc. or its affiliates
#
# SPDX-License-Identifier: Apache-2.0

from enum import Enum


class UriScheme(Enum):
    S3 = "s3"
    S3_ZIP = "s3+zip"
    S3_LAZY = "s3+lazy"
