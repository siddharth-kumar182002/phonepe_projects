``` mermaid
graph TD
    A[Start] --> B(Initialize: Parse Args, Load Config, Setup Logger, Audit Start);
    B --> C{Pre-Checks};

    subgraph C [Pre-Checks]
        direction TB
        C1[Check ZK Quorum] --> C2{OK?};
        C2 -- Yes --> C3[Check Active Master];
        C3 --> C4{OK?};
        C4 -- Yes --> C5[Check Table Exists];
        C5 --> C6{Exists?};
        C6 -- No --> C_OUT(Pre-Checks Passed);
        C6 -- Yes --> EXIT_EXISTS[Log Error & Exit 1];
        C2 -- No --> ERROR(Error & Exit 1);
        C4 -- No --> ERROR;
    end

    C_OUT --> D[Create Table via HappyBase];
    D --> E["Brief Wait (time.sleep)"];
    E --> F[Post-Check: Verify Table Exists];
    F --> G{Exists?};
    G -- Yes --> SUCCESS_FINAL[Log Success, Audit, Report];
    G -- No --> ERROR;

    SUCCESS_FINAL --> H(Run Finally Block);
    H --> I["Close HappyBase Connection (if open)"];
    I --> Z["End (Success)"];

    ERROR --> X(Handle Exception);
    X --> X1[Log Error];
    X1 --> X2[Audit Failure];
    X2 --> X3[Write Failure Report];
    X3 --> H; 
    %% X3 --> X4[Exit 1]; Exit happens after Finally in Python

    %% Style Adjustments (Optional)
    style ERROR fill:#f9f,stroke:#333,stroke-width:2px
    style EXIT_EXISTS fill:#ffcc00,stroke:#333,stroke-width:2px
    style SUCCESS_FINAL fill:#ccf,stroke:#333,stroke-width:2px
    ```