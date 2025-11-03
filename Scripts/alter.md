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
        C6 -- Yes --> C7[Check RIT Count];
        C7 --> C8{RIT <= Max?};
        C8 -- Yes --> C9["Check Balancer State (Log Only)"];
        C9 --> C_OUT(Pre-Checks Passed);
        C2 -- No --> ERROR(Error & Exit 1);
        C4 -- No --> ERROR;
        C6 -- No --> ERROR;
        C8 -- No --> ERROR;
    end

    C_OUT --> D[Call alter_table_salt];
    subgraph D ["alter_table_salt Function (Simplified)"]
        D1(Check & Disable Balancer if Enabled) --> D2(Disable Table via Salt/Shell);
        D2 --> D3(Alter Table via Salt/Shell);
        D3 --> D4(Enable Table via Salt/Shell);
        D4 --> D5(Restore Original Balancer State);
        %% Internal error handling within alter_table_salt is complex, represented by main error path %%
    end

    D --> E[Post-Check: Verify Table Enabled];
    subgraph E [Verify Table Enabled]
        direction TB
        E1[Retry Loop] --> E2[Use HappyBase: is_table_enabled?];
        E2 --> E3{Enabled?};
        E3 -- Yes --> E_OUT1(Verification Success);
        E3 -- No --> E4{Retries Left?};
        E4 -- Yes --> E1;
        E4 -- No --> E_OUT2(Verification Failed);
    end
    E_OUT1 --> SUCCESS_FINAL[Log Success, Audit, Report];
    E_OUT2 --> ERROR; 

    SUCCESS_FINAL --> F(Run Finally Block);
    F --> Z["End (Success)"];

    ERROR --> X(Handle Exception);
    X --> X1[Log Error];
    X1 --> X2[Audit Failure];
    X2 --> X3[Write Failure Report];
    X3 --> X4[Exit 1];

    FINALLY[Finally Block Logic] --> FIN_OUT(No explicit action);

    %% Link main flow to Finally block just before ending
    F --> FINALLY;
    %% Link error handling (though Exit 1 happens before finally block)

    %% Style Adjustments (Optional)
    style ERROR fill:#f9f,stroke:#333,stroke-width:2px
    style SUCCESS_FINAL fill:#ccf,stroke:#333,stroke-width:2px
    ```